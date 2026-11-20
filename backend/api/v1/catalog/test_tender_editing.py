from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.catalog.tests import _jwt_bearer
from catalog.models import MasterProduct, Tender, TenderAccess, TenderItem, TenderStatus
from core.models import EntityStatus, Unit, User, UserRole, Venue, VenueUser, VenueUserRole
from suppliers.models import Supplier


class TenderEditingFixture(APITestCase):
    """Raw ТЗ §16, docs/adr/0009: an active tender is edited only by extending it."""

    def setUp(self):
        self.owner = User.objects.create(email="edit-owner@example.com", full_name="Owner", role=UserRole.VENUE_USER)
        self.stranger = User.objects.create(
            email="edit-stranger@example.com", full_name="Stranger", role=UserRole.VENUE_USER
        )
        self.venue = Venue.objects.create(display_name="Cafe Edit")
        self.other_venue = Venue.objects.create(display_name="Cafe Edit 2")
        for venue in (self.venue, self.other_venue):
            VenueUser.objects.create(venue=venue, user=self.owner, role=VenueUserRole.OWNER, status=EntityStatus.ACTIVE)
        self.foreign_venue = Venue.objects.create(display_name="Чужое кафе")
        self.supplier_a = Supplier.objects.create(legal_name="Edit A LLC", display_name="Edit A")
        self.supplier_b = Supplier.objects.create(legal_name="Edit B LLC", display_name="Edit B")
        self.deadline = timezone.now() + timedelta(days=3)
        self.tender = Tender.objects.create(
            created_by=self.owner,
            name="Осеннее меню",
            description="Условия",
            access=TenderAccess.INVITE_ONLY,
            accepts_offers_until=self.deadline,
            status=TenderStatus.ACTIVE,
        )
        self.tender.invited_suppliers.set([self.supplier_a])
        unit = Unit.objects.create(code="l-edit", name="литр")
        self.master_product = MasterProduct.objects.create(owner=self.owner, canonical_name="Сливки 33%", unit=unit)
        self.item = TenderItem.objects.create(
            tender=self.tender, master_product=self.master_product, comment="33-35%", product_kind_label="Сливки"
        )
        self.item.venues.set([self.venue])
        self.tender_url = f"/api/v1/catalog/tenders/{self.tender.id}"
        self.item_url = f"{self.tender_url}/items/{self.item.id}"

    def auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_bearer(user))

    def patch(self, url, body, user=None):
        self.auth(user or self.owner)
        return self.client.patch(url, body, format="json")


class TenderPatchAPITests(TenderEditingFixture):
    def test_requires_authentication(self):
        response = self.client.patch(self.tender_url, {"name": "Новое"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_other_owner_gets_403(self):
        response = self.patch(self.tender_url, {"name": "Новое"}, user=self.stranger)
        self.assertEqual(response.status_code, 403)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.name, "Осеннее меню")

    def test_rename_and_describe_active_tender(self):
        response = self.patch(self.tender_url, {"name": "Зимнее меню", "description": "Новые условия"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["name"], "Зимнее меню")
        self.assertEqual(data["description"], "Новые условия")
        self.assertEqual(data["items_count"], 1)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.name, "Зимнее меню")

    def test_blank_name_rejected(self):
        response = self.patch(self.tender_url, {"name": "  "})
        self.assertEqual(response.status_code, 400, response.content)

    def test_unchanged_form_is_accepted(self):
        body = {
            "name": "Осеннее меню",
            "description": "Условия",
            "accepts_offers_until": self.deadline.isoformat(),
            "access": TenderAccess.INVITE_ONLY,
            "invited_suppliers": [str(self.supplier_a.id)],
        }
        response = self.patch(self.tender_url, body)
        self.assertEqual(response.status_code, 200, response.content)

    def test_deadline_extended_by_a_minute(self):
        later = self.deadline + timedelta(minutes=1)
        response = self.patch(self.tender_url, {"accepts_offers_until": later.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.accepts_offers_until, later)

    def test_deadline_shortened_by_a_minute_rejected(self):
        earlier = self.deadline - timedelta(minutes=1)
        response = self.patch(self.tender_url, {"accepts_offers_until": earlier.isoformat()})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("accepts_offers_until", response.json())
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.accepts_offers_until, self.deadline)

    def test_deadline_removed(self):
        response = self.patch(self.tender_url, {"accepts_offers_until": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.tender.refresh_from_db()
        self.assertIsNone(self.tender.accepts_offers_until)

    def test_deadline_set_on_open_ended_tender_rejected(self):
        self.tender.accepts_offers_until = None
        self.tender.save(update_fields=["accepts_offers_until"])
        response = self.patch(self.tender_url, {"accepts_offers_until": self.deadline.isoformat()})
        self.assertEqual(response.status_code, 400, response.content)
        self.tender.refresh_from_db()
        self.assertIsNone(self.tender.accepts_offers_until)

    def test_invited_supplier_added(self):
        body = {"invited_suppliers": [str(self.supplier_a.id), str(self.supplier_b.id)]}
        response = self.patch(self.tender_url, body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertCountEqual(
            response.json()["invited_supplier_ids"], [str(self.supplier_a.id), str(self.supplier_b.id)]
        )

    def test_invited_supplier_removal_rejected(self):
        response = self.patch(self.tender_url, {"invited_suppliers": [str(self.supplier_b.id)]})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("invited_suppliers", response.json())
        self.assertEqual(list(self.tender.invited_suppliers.all()), [self.supplier_a])

    def test_invited_suppliers_on_open_tender_rejected(self):
        self.tender.access = TenderAccess.OPEN
        self.tender.save(update_fields=["access"])
        self.tender.invited_suppliers.clear()
        response = self.patch(self.tender_url, {"invited_suppliers": [str(self.supplier_a.id)]})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(self.tender.invited_suppliers.exists())

    def test_access_change_rejected(self):
        response = self.patch(self.tender_url, {"access": TenderAccess.OPEN})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("access", response.json())
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.access, TenderAccess.INVITE_ONLY)

    def test_completed_tender_rejected(self):
        self.tender.status = TenderStatus.COMPLETED
        self.tender.save(update_fields=["status"])
        response = self.patch(self.tender_url, {"name": "Поздно"})
        self.assertEqual(response.status_code, 400, response.content)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.name, "Осеннее меню")

    def test_passed_deadline_completes_tender_before_edit(self):
        Tender.objects.filter(pk=self.tender.pk).update(accepts_offers_until=timezone.now() - timedelta(minutes=1))
        later = timezone.now() + timedelta(days=1)
        response = self.patch(self.tender_url, {"accepts_offers_until": later.isoformat()})
        self.assertEqual(response.status_code, 400, response.content)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, TenderStatus.COMPLETED)


class TenderItemPatchAPITests(TenderEditingFixture):
    def test_requires_authentication(self):
        response = self.client.patch(self.item_url, {"comment": "x"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_other_owner_gets_403(self):
        response = self.patch(self.item_url, {"comment": "x"}, user=self.stranger)
        self.assertEqual(response.status_code, 403)

    def test_item_of_another_tender_is_404(self):
        other = Tender.objects.create(created_by=self.owner, name="Другой", access=TenderAccess.OPEN)
        response = self.patch(f"/api/v1/catalog/tenders/{other.id}/items/{self.item.id}", {"comment": "x"})
        self.assertEqual(response.status_code, 404, response.content)

    def test_comment_and_kind_changed_on_active_tender(self):
        response = self.patch(self.item_url, {"comment": "Жирность 33%", "product_kind_label": "Молочка"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["comment"], "Жирность 33%")
        self.assertEqual(data["product_kind_label"], "Молочка")
        self.assertEqual(data["offers_count"], 0)

    def test_venue_added(self):
        body = {"venue_ids": [str(self.venue.id), str(self.other_venue.id)]}
        response = self.patch(self.item_url, body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertCountEqual(response.json()["venue_ids"], [str(self.venue.id), str(self.other_venue.id)])

    def test_unchanged_venues_accepted(self):
        response = self.patch(self.item_url, {"venue_ids": [str(self.venue.id)], "comment": "33-35%"})
        self.assertEqual(response.status_code, 200, response.content)

    def test_venue_removal_rejected(self):
        response = self.patch(self.item_url, {"venue_ids": [str(self.other_venue.id)]})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("venue_ids", response.json())
        self.assertEqual(list(self.item.venues.all()), [self.venue])

    def test_all_venues_removed_rejected(self):
        response = self.patch(self.item_url, {"venue_ids": []})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(list(self.item.venues.all()), [self.venue])

    def test_foreign_venue_rejected(self):
        body = {"venue_ids": [str(self.venue.id), str(self.foreign_venue.id)]}
        response = self.patch(self.item_url, body)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(list(self.item.venues.all()), [self.venue])

    def test_venue_held_by_another_active_tender_is_409(self):
        other = Tender.objects.create(
            created_by=self.owner, name="Тендер №15", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        other_item = TenderItem.objects.create(tender=other, master_product=self.master_product)
        other_item.venues.set([self.other_venue])
        body = {"venue_ids": [str(self.venue.id), str(self.other_venue.id)], "comment": "не сохранится"}
        response = self.patch(self.item_url, body)
        self.assertEqual(response.status_code, 409, response.content)
        conflicts = response.json()["conflicts"]
        self.assertEqual([c["venue_id"] for c in conflicts], [str(self.other_venue.id)])
        self.item.refresh_from_db()
        self.assertEqual(self.item.comment, "33-35%")
        self.assertEqual(list(self.item.venues.all()), [self.venue])

    def test_completed_tender_rejected(self):
        self.tender.status = TenderStatus.COMPLETED
        self.tender.save(update_fields=["status"])
        response = self.patch(self.item_url, {"comment": "Поздно"})
        self.assertEqual(response.status_code, 400, response.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.comment, "33-35%")


class TenderDraftEditingAPITests(TenderEditingFixture):
    """Raw ТЗ §14: a draft is invisible to suppliers, so it is edited freely."""

    def setUp(self):
        super().setUp()
        self.tender.status = TenderStatus.DRAFT
        self.tender.save(update_fields=["status"])

    def test_deadline_shortened(self):
        earlier = self.deadline - timedelta(days=1)
        response = self.patch(self.tender_url, {"accepts_offers_until": earlier.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.accepts_offers_until, earlier)

    def test_deadline_set_on_open_ended_draft(self):
        self.tender.accepts_offers_until = None
        self.tender.save(update_fields=["accepts_offers_until"])
        response = self.patch(self.tender_url, {"accepts_offers_until": self.deadline.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.accepts_offers_until, self.deadline)

    def test_invited_supplier_replaced(self):
        response = self.patch(self.tender_url, {"invited_suppliers": [str(self.supplier_b.id)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["invited_supplier_ids"], [str(self.supplier_b.id)])

    def test_empty_invites_on_invite_only_rejected(self):
        response = self.patch(self.tender_url, {"invited_suppliers": []})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(list(self.tender.invited_suppliers.all()), [self.supplier_a])

    def test_switch_to_open_clears_invites(self):
        response = self.patch(self.tender_url, {"access": TenderAccess.OPEN})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["access"], TenderAccess.OPEN)
        self.assertEqual(data["invited_supplier_ids"], [])

    def test_switch_to_invite_only_needs_suppliers(self):
        self.tender.access = TenderAccess.OPEN
        self.tender.save(update_fields=["access"])
        self.tender.invited_suppliers.clear()
        response = self.patch(self.tender_url, {"access": TenderAccess.INVITE_ONLY})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("invited_suppliers", response.json())
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.access, TenderAccess.OPEN)

    def test_switch_to_invite_only_with_suppliers(self):
        self.tender.access = TenderAccess.OPEN
        self.tender.save(update_fields=["access"])
        self.tender.invited_suppliers.clear()
        body = {"access": TenderAccess.INVITE_ONLY, "invited_suppliers": [str(self.supplier_b.id)]}
        response = self.patch(self.tender_url, body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["invited_supplier_ids"], [str(self.supplier_b.id)])

    def test_item_venue_replaced(self):
        response = self.patch(self.item_url, {"venue_ids": [str(self.other_venue.id)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["venue_ids"], [str(self.other_venue.id)])

    def test_item_all_venues_removed(self):
        response = self.patch(self.item_url, {"venue_ids": []})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["venue_ids"], [])

    def test_item_venue_held_by_active_tender_checked_at_publish(self):
        other = Tender.objects.create(
            created_by=self.owner, name="Тендер №15", access=TenderAccess.OPEN, status=TenderStatus.ACTIVE
        )
        TenderItem.objects.create(tender=other, master_product=self.master_product).venues.set([self.other_venue])
        response = self.patch(self.item_url, {"venue_ids": [str(self.venue.id), str(self.other_venue.id)]})
        self.assertEqual(response.status_code, 200, response.content)

    def test_item_foreign_venue_rejected(self):
        response = self.patch(self.item_url, {"venue_ids": [str(self.foreign_venue.id)]})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(list(self.item.venues.all()), [self.venue])


class TenderItemDeleteAPITests(TenderEditingFixture):
    """Raw ТЗ §14: a position is deleted only from a draft; a published one is unpublished instead (§18)."""

    def setUp(self):
        super().setUp()
        self.tender.status = TenderStatus.DRAFT
        self.tender.save(update_fields=["status"])

    def delete(self, url, user=None):
        self.auth(user or self.owner)
        return self.client.delete(url)

    def test_requires_authentication(self):
        response = self.client.delete(self.item_url)
        self.assertEqual(response.status_code, 401)
        self.assertTrue(TenderItem.objects.filter(pk=self.item.pk).exists())

    def test_other_owner_gets_403(self):
        response = self.delete(self.item_url, user=self.stranger)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(TenderItem.objects.filter(pk=self.item.pk).exists())

    def test_item_of_another_tender_is_404(self):
        other = Tender.objects.create(created_by=self.owner, name="Другой", access=TenderAccess.OPEN)
        response = self.delete(f"/api/v1/catalog/tenders/{other.id}/items/{self.item.id}")
        self.assertEqual(response.status_code, 404, response.content)
        self.assertTrue(TenderItem.objects.filter(pk=self.item.pk).exists())

    def test_draft_item_deleted(self):
        response = self.delete(self.item_url)
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(TenderItem.objects.filter(pk=self.item.pk).exists())
        self.assertEqual(self.client.get(self.tender_url).json()["items_count"], 0)

    def test_active_item_rejected(self):
        self.tender.status = TenderStatus.ACTIVE
        self.tender.save(update_fields=["status"])
        response = self.delete(self.item_url)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertTrue(TenderItem.objects.filter(pk=self.item.pk).exists())

    def test_completed_item_rejected(self):
        self.tender.status = TenderStatus.COMPLETED
        self.tender.save(update_fields=["status"])
        response = self.delete(self.item_url)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertTrue(TenderItem.objects.filter(pk=self.item.pk).exists())
