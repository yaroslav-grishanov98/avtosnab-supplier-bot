from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderItem, TenderStatus
from core.models import EntityStatus, Unit, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier, SupplierUser, SupplierUserRole


class TenderHistoryAPITests(APITestCase):
    """Raw ТЗ §16 (who, when, old and new value) and §76 (tender: created, published, changed, completed),
    docs/adr/0010. Draft edits are not recorded."""

    def setUp(self):
        self.owner = User.objects.create(
            email="hist-owner@example.com", full_name="Анна Покупатель", role=UserRole.VENUE_USER
        )
        self.manager = User.objects.create(
            email="hist-mgr@example.com", full_name="Борис Менеджер", role=UserRole.VENUE_USER
        )
        self.stranger = User.objects.create(email="hist-str@example.com", full_name="Чужой", role=UserRole.VENUE_USER)
        self.cafe_1 = Venue.objects.create(display_name="Кафе 1")
        self.cafe_2 = Venue.objects.create(display_name="Кафе 2")
        for venue in (self.cafe_1, self.cafe_2):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        VenueUser.objects.create(
            venue=self.cafe_1, user=self.manager, role=VenueUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.supplier_a = Supplier.objects.create(legal_name="A LLC", display_name="Поставщик А")
        self.supplier_b = Supplier.objects.create(legal_name="B LLC", display_name="Поставщик Б")
        unit = Unit.objects.create(code="l-hist", name="литр")
        self.cream = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%", unit=unit)
        self.cheese = MasterProduct.objects.create(owner=self.owner, canonical_name="Камамбер", unit=unit)
        self.deadline = timezone.now() + timedelta(days=3)

    def auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user or self.owner))

    def active_tender(self):
        tender = Tender.objects.create(
            created_by=self.owner,
            name="Осеннее меню",
            description="Условия",
            access=TenderAccess.INVITE_ONLY,
            accepts_offers_until=self.deadline,
            status=TenderStatus.ACTIVE,
        )
        tender.invited_suppliers.set([self.supplier_a])
        item = TenderItem.objects.create(tender=tender, master_product=self.cream, comment="33-35%")
        item.venues.set([self.cafe_1])
        return tender, item

    def history(self, tender, user=None):
        self.auth(user)
        response = self.client.get(f"/api/v1/catalog/tenders/{tender.id}/history")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def changes(self, rows):
        return [(row["change_type"], row["changed_field"], row["old_value"], row["new_value"]) for row in rows]

    def test_requires_authentication(self):
        tender, _ = self.active_tender()
        response = self.client.get(f"/api/v1/catalog/tenders/{tender.id}/history")
        self.assertEqual(response.status_code, 401)

    def test_other_owner_gets_403(self):
        tender, _ = self.active_tender()
        self.auth(self.stranger)
        response = self.client.get(f"/api/v1/catalog/tenders/{tender.id}/history")
        self.assertEqual(response.status_code, 403)

    def test_lifecycle_newest_first_and_draft_edits_skipped(self):
        self.auth()
        response = self.client.post(
            "/api/v1/catalog/tenders",
            {
                "owner_id": str(self.owner.id),
                "name": "Черновик",
                "access": TenderAccess.OPEN,
                "items": [{"master_product_id": str(self.cream.id), "venue_ids": [str(self.cafe_1.id)]}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        tender_id = response.json()["id"]
        prefix = f"/api/v1/catalog/tenders/{tender_id}"
        item_id = self.client.get(f"{prefix}/items").json()[0]["id"]
        self.assertEqual(self.client.patch(prefix, {"name": "Осеннее меню"}, format="json").status_code, 200)
        self.assertEqual(
            self.client.patch(f"{prefix}/items/{item_id}", {"comment": "жирность"}, format="json").status_code, 200
        )
        self.assertEqual(self.client.post(f"{prefix}/publish").status_code, 200)
        self.assertEqual(self.client.post(f"{prefix}/complete").status_code, 200)

        rows = self.history(Tender.objects.get(pk=tender_id))
        self.assertEqual([row["change_type"] for row in rows], ["completed", "published", "created"])
        for row in rows:
            self.assertEqual(row["actor_id"], str(self.owner.id))
            self.assertEqual(row["actor_name"], "Анна Покупатель")

    def test_tender_edit_one_row_per_changed_field(self):
        tender, _ = self.active_tender()
        later = self.deadline + timedelta(days=1)
        self.auth()
        response = self.client.patch(
            f"/api/v1/catalog/tenders/{tender.id}",
            {
                "name": "Зимнее меню",
                "description": "Условия",
                "accepts_offers_until": later.isoformat(),
                "invited_suppliers": [str(self.supplier_a.id), str(self.supplier_b.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        rows = self.history(tender)
        self.assertCountEqual(
            [
                (change_type, field, old, new)
                for change_type, field, old, new in self.changes(rows)
                if field != "accepts_offers_until"
            ],
            [
                ("updated", "name", "Осеннее меню", "Зимнее меню"),
                ("updated", "invited_suppliers", "Поставщик А", "Поставщик А, Поставщик Б"),
            ],
        )
        deadline_row = next(row for row in rows if row["changed_field"] == "accepts_offers_until")
        self.assertEqual(parse_datetime(deadline_row["old_value"]), self.deadline)
        self.assertEqual(parse_datetime(deadline_row["new_value"]), later)
        self.assertTrue(all(row["tender_item_id"] is None for row in rows))

    def test_item_edit_rows_name_the_item(self):
        tender, item = self.active_tender()
        self.auth()
        response = self.client.patch(
            f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}",
            {
                "comment": "Жирность 33%",
                "product_kind_label": "Молочка",
                "venue_ids": [str(self.cafe_1.id), str(self.cafe_2.id)],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        rows = self.history(tender)
        self.assertCountEqual(
            self.changes(rows),
            [
                ("item_updated", "comment", "33-35%", "Жирность 33%"),
                ("item_updated", "product_kind_label", None, "Молочка"),
                ("item_updated", "venues", "Кафе 1", "Кафе 1, Кафе 2"),
            ],
        )
        for row in rows:
            self.assertEqual(row["tender_item_id"], str(item.id))
            self.assertEqual(row["item_name"], "Сливки 33%")

    def test_item_added_and_publication_toggled(self):
        tender, item = self.active_tender()
        prefix = f"/api/v1/catalog/tenders/{tender.id}"
        self.auth()
        response = self.client.post(
            f"{prefix}/items",
            {"items": [{"master_product_id": str(self.cheese.id), "venue_ids": [str(self.cafe_2.id)]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.client.post(f"{prefix}/items/{item.id}/unpublish").status_code, 200)
        self.assertEqual(self.client.post(f"{prefix}/items/{item.id}/unpublish").status_code, 200)
        self.assertEqual(self.client.post(f"{prefix}/items/{item.id}/publish").status_code, 200)

        rows = self.history(tender)
        self.assertEqual(
            [(row["change_type"], row["item_name"]) for row in rows],
            [("item_published", "Сливки 33%"), ("item_unpublished", "Сливки 33%"), ("item_added", "Камамбер")],
        )

    def test_actor_is_the_token_user(self):
        tender, _ = self.active_tender()
        self.auth(self.manager)
        response = self.client.patch(f"/api/v1/catalog/tenders/{tender.id}", {"name": "От менеджера"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

        row = self.history(tender)[0]
        self.assertEqual(row["actor_id"], str(self.manager.id))
        self.assertEqual(row["actor_name"], "Борис Менеджер")

    def test_rejected_edit_writes_nothing(self):
        tender, _ = self.active_tender()
        self.auth()
        response = self.client.patch(
            f"/api/v1/catalog/tenders/{tender.id}",
            {"name": "Не сохранится", "accepts_offers_until": (self.deadline - timedelta(minutes=1)).isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.history(tender), [])

    def test_unchanged_form_writes_nothing(self):
        tender, item = self.active_tender()
        self.auth()
        self.client.patch(
            f"/api/v1/catalog/tenders/{tender.id}",
            {"name": "Осеннее меню", "invited_suppliers": [str(self.supplier_a.id)]},
            format="json",
        )
        self.client.patch(
            f"/api/v1/catalog/tenders/{tender.id}/items/{item.id}",
            {"comment": "33-35%", "venue_ids": [str(self.cafe_1.id)]},
            format="json",
        )
        self.assertEqual(self.history(tender), [])

    def test_deadline_completion_recorded_once_without_actor(self):
        tender, _ = self.active_tender()
        passed = timezone.now() - timedelta(minutes=1)
        Tender.objects.filter(pk=tender.pk).update(accepts_offers_until=passed)
        self.history(tender)

        rows = self.history(tender)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["change_type"], "completed")
        self.assertIsNone(rows[0]["actor_id"])
        self.assertIsNone(rows[0]["actor_name"])
        self.assertEqual(parse_datetime(rows[0]["occurred_at"]), passed)


class SupplierTenderHistoryAPITests(APITestCase):
    """Product owner 2026-09-28: suppliers see the tender history too; §51 — nothing about other suppliers, and the
    buyer's staff stay anonymous (docs/adr/0010)."""

    def setUp(self):
        self.owner = User.objects.create(
            email="sh-owner@example.com", full_name="Анна Покупатель", role=UserRole.VENUE_USER
        )
        cafe = Venue.objects.create(display_name="Кафе 1")
        VenueUser.objects.create(venue=cafe, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.supplier = Supplier.objects.create(legal_name="Sh LLC", display_name="Поставщик А")
        self.rival = Supplier.objects.create(legal_name="Rival LLC", display_name="Поставщик Б")
        self.member = User.objects.create(email="sh-sup@example.com", full_name="Сергей", role=UserRole.SUPPLIER)
        SupplierUser.objects.create(
            supplier=self.supplier, user=self.member, role=SupplierUserRole.MANAGER, status=EntityStatus.ACTIVE
        )
        self.tender = Tender.objects.create(
            created_by=self.owner,
            name="Осеннее меню",
            access=TenderAccess.INVITE_ONLY,
            status=TenderStatus.ACTIVE,
            accepts_offers_until=timezone.now() + timedelta(days=3),
        )
        self.tender.invited_suppliers.set([self.supplier])
        cream = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки")
        TenderItem.objects.create(tender=self.tender, master_product=cream).venues.set([cafe])
        self.url = f"/api/v1/catalog/suppliers/{self.supplier.id}/tenders/{self.tender.id}/history"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def edit_as_owner(self):
        self.auth(self.owner)
        response = self.client.patch(
            f"/api/v1/catalog/tenders/{self.tender.id}",
            {"name": "Зимнее меню", "invited_suppliers": [str(self.supplier.id), str(self.rival.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_non_member_gets_403(self):
        stranger = User.objects.create(email="sh-str@example.com", full_name="Чужой", role=UserRole.SUPPLIER)
        self.auth(stranger)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_changes_without_invites_and_without_buyer_names(self):
        self.edit_as_owner()
        self.auth(self.member)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        self.assertEqual(
            [(row["change_type"], row["changed_field"], row["old_value"], row["new_value"]) for row in rows],
            [("updated", "name", "Осеннее меню", "Зимнее меню")],
        )
        self.assertEqual((rows[0]["actor_id"], rows[0]["actor_name"]), (None, None))
        self.assertNotIn("Поставщик Б", response.content.decode())

    def test_buyer_still_sees_invites_and_names(self):
        self.edit_as_owner()
        self.auth(self.owner)
        rows = self.client.get(f"/api/v1/catalog/tenders/{self.tender.id}/history").json()
        self.assertCountEqual([row["changed_field"] for row in rows], ["name", "invited_suppliers"])
        self.assertEqual({row["actor_name"] for row in rows}, {"Анна Покупатель"})

    def test_not_invited_supplier_gets_404(self):
        self.tender.invited_suppliers.clear()
        self.auth(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_draft_is_404(self):
        Tender.objects.filter(pk=self.tender.pk).update(status=TenderStatus.DRAFT)
        self.auth(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_events_of_unpublished_items_hidden(self):
        hidden_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Скрытый товар")
        shown_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Видимый товар")
        cafe = Venue.objects.get(display_name="Кафе 1")
        hidden = TenderItem.objects.create(tender=self.tender, master_product=hidden_product, comment="старое")
        shown = TenderItem.objects.create(tender=self.tender, master_product=shown_product, comment="старое")
        for item in (hidden, shown):
            item.venues.set([cafe])
        self.auth(self.owner)
        for item in (hidden, shown):
            url = f"/api/v1/catalog/tenders/{self.tender.id}/items/{item.id}"
            self.assertEqual(self.client.patch(url, {"comment": "новое"}, format="json").status_code, 200)
        unpublish = f"/api/v1/catalog/tenders/{self.tender.id}/items/{hidden.id}/unpublish"
        self.assertEqual(self.client.post(unpublish).status_code, 200)

        self.auth(self.member)
        rows = self.client.get(self.url).json()
        self.assertEqual([(row["change_type"], row["item_name"]) for row in rows], [("item_updated", "Видимый товар")])
