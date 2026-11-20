from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

IMAGE_FIELDS = {"id", "image_url", "position", "created_at"}


class SupplierProductImageOpenAPIContractTests(SimpleTestCase):
    """The product list and the gallery endpoints return the same image rows; the schema
    used to describe them with two different classes of the same name (no created_at)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def ref_name(self, schema):
        return schema["$ref"].rsplit("/", 1)[-1]

    def test_image_component_lists_every_returned_field(self):
        component = self.schema["components"]["schemas"]["SupplierProductImage"]
        self.assertEqual(set(component["properties"]), IMAGE_FIELDS)
        self.assertEqual(set(component["required"]), IMAGE_FIELDS)
        self.assertEqual(component["properties"]["created_at"]["format"], "date-time")

    def test_product_list_and_gallery_use_the_same_component(self):
        list_item = self.schema["components"]["schemas"]["SupplierProductListItem"]
        self.assertEqual(self.ref_name(list_item["properties"]["images"]["items"]), "SupplierProductImage")

        gallery = self.schema["paths"]["/api/v1/suppliers/products/{id}/image"]
        listed = gallery["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        created = gallery["post"]["responses"]["201"]["content"]["application/json"]["schema"]
        self.assertEqual(self.ref_name(listed["items"]), "SupplierProductImage")
        self.assertEqual(self.ref_name(created), "SupplierProductImage")
