from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from django.test import SimpleTestCase

from api.v1.suppliers.serializers import SupplierPriceUploadResponseSerializer
from suppliers.models import SourceChannel, UploadStatus


class SupplierPriceUploadFrontendFlowTests(SimpleTestCase):
    def serialize(self, status):
        upload_id = uuid4()
        upload = SimpleNamespace(
            id=upload_id,
            supplier_id=uuid4(),
            source_channel=SourceChannel.WEB,
            source_file_name="price.xlsx",
            source_file_uri="supplier_uploads/price.xlsx",
            status=status,
            uploaded_at=datetime.now(timezone.utc),
            raw_payload={},
        )
        return upload_id, SupplierPriceUploadResponseSerializer(upload).data

    def test_received_upload_points_frontend_to_preview(self):
        upload_id, data = self.serialize(UploadStatus.RECEIVED)
        self.assertEqual(data["next_action"], "preview")
        self.assertEqual(data["status_url"], f"/api/v1/suppliers/uploads/{upload_id}")
        self.assertEqual(data["preview_url"], f"/api/v1/suppliers/uploads/{upload_id}/preview")
        self.assertEqual(data["publish_url"], f"/api/v1/suppliers/uploads/{upload_id}/publish")
        self.assertEqual(data["action_urls"]["review_items"], f"/api/v1/suppliers/uploads/{upload_id}/items")
        self.assertEqual(data["available_actions"], ["preview", "publish", "reject"])

    def test_processing_upload_tells_frontend_to_poll(self):
        _upload_id, data = self.serialize(UploadStatus.PROCESSING)
        self.assertEqual(data["next_action"], "poll")
        self.assertEqual(data["available_actions"], ["poll"])

    def test_needs_review_upload_exposes_review_actions(self):
        _upload_id, data = self.serialize(UploadStatus.NEEDS_REVIEW)
        self.assertEqual(data["next_action"], "review_items")
        self.assertEqual(data["available_actions"], ["review_items", "publish", "reject"])
