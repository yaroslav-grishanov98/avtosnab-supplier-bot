# Data-only migration, no schema change.
#
# 2026-08-14: живая проверка GET /suppliers/categories на проде (см.
# docs/wiki/logs/2026-08-14-category-taxonomy-cleanup.md) нашла 22 мусорных узла
# дерева категорий, двух видов:
#
# 1. Имя категории — это буквально id другой, настоящей категории. Причина —
#    SupplierCategorySerializer.id раньше был подписан "сюда передавать элементом
#    category_path", хотя category_path резолвится по имени
#    (_resolve_category_path() в api/v1/suppliers/services.py), не по id. Кто-то
#    последовал этому совету буквально — так появились категории с именами вроде
#    "45cbcd3a-0292-4693-a6c0-d72c54bde09f" (= id категории "Бакалея"). help_text
#    исправлен отдельно (api/v1/suppliers/serializers.py).
#
# 2. Явный ручной тест через форму создания товара: "ffff", "йцу", "тест1"/
#    "тест1.1"/"тест1.2"/"тест1.3", "ыва"/"выа", "qwe" — с дочерними узлами того же
#    происхождения.
#
# Проверено перед написанием миграции (SSH-доступ к прод-БД, read-only): ровно 4
# товара ссылались на эти категории — все статус archived, все имена буквально
# "тест6"/"qweee"/"ываыва1"/"тест22" от одного тестового поставщика. Ни одного
# реального товара/заказа не задето. SupplierProduct.category — on_delete=SET_NULL,
# так что эти 4 архивных товара просто потеряют ссылку на категорию, не будут
# удалены сами.
#
# ProductCategory.parent тоже SET_NULL, не CASCADE — поэтому здесь удаляются id
# целых поддеревьев (группа + все её мусорные подгруппы/виды), не только корневые
# узлы, иначе дочерние узлы осиротели бы и всплыли как новые фиктивные "группы".
from django.db import migrations

JUNK_CATEGORY_IDS = [
    "087c7c84-52e4-4558-9cf5-55aef2ea99ec",  # тест1.2 (вид)
    "0b5dee48-542c-46d2-9a62-f6ca46cb903b",  # qwe (подгруппа, под "йцу")
    "161a10b7-3c7f-44c4-9476-c2ce98f44118",  # ыва (группа)
    "2721de5a-d217-434d-a260-36d7f0cc773e",  # выа (вид, под "ыва")
    "2bc9ac0a-f3e8-4e64-8678-5783879581a1",  # тест1.1 (подгруппа)
    "2f92c1c8-65ff-490b-a181-62d03382eb8d",  # тест1 (группа)
    "3fcd2603-c381-40a9-b261-56717f2540bf",  # id-коллизия с "Водка" (вид)
    "403213ee-6a50-450f-b850-ca1d03c7df90",  # id-коллизия с "Алкоголь" (группа)
    "40de71c4-e0e1-4635-9c8d-bc87770788ba",  # qwe (вид, под "йцу"->"qwe")
    "5d19ae95-397d-4e1d-992f-fd7889618683",  # тест1.1 (вид)
    "65da2aba-5427-471c-88df-e7f65dc81a45",  # id-коллизия с "крупы" (подгруппа)
    "6bae426d-17d7-4673-b023-e31a98f4f584",  # тест1 (вид)
    "79b3262f-ec97-4864-ad6e-d2939dedfe7b",  # id-коллизия с "рис" (вид)
    "86d43a66-d3c2-45fa-80d9-c3eb31cd5687",  # йцу (группа)
    "c9edacaf-c3c0-455d-bef9-87cc250a2306",  # ыва (подгруппа)
    "cae94b71-98b6-4ee2-9dbd-4d4e0878e1e9",  # тест1.2 (подгруппа)
    "cd0c17ae-3d3b-442a-b74e-eacc18ffa140",  # тест1 (подгруппа)
    "cd6fadb3-854a-4750-aa35-a3d88bb290ce",  # ffff (группа)
    "d086c75d-6f3b-48e1-a76b-5bb215fc00ed",  # тест1.3 (подгруппа)
    "d25722b1-7d2c-4afa-ad09-0684df9d49e9",  # тест1.3 (вид)
    "d5c9526e-811d-4fe0-99ad-9f9c55148587",  # id-коллизия с "Крепкий алкоголь" (подгруппа)
    "dc26bc1c-6449-49fc-9aad-1f9b54fb39e2",  # id-коллизия с "Бакалея" (группа)
]


def delete_junk_categories(apps, schema_editor):
    ProductCategory = apps.get_model("catalog", "ProductCategory")
    ProductCategory.objects.filter(id__in=JUNK_CATEGORY_IDS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0008_seed_category_taxonomy"),
    ]

    operations = [
        migrations.RunPython(delete_junk_categories, migrations.RunPython.noop),
    ]
