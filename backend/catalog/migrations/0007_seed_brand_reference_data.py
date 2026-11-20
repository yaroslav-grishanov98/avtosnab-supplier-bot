# Data-only migration, no schema change.
#
# 2026-08-11/12: созвон 2026-08-10 — Павел попросил спарсить список брендов с нескольких
# сайтов-аналогов (упомянут GFC) для автокомплита при вводе brand в форме товара
# (GET /suppliers/brands?q=..., см. api/v1/suppliers/views.py). Источник — gfc-russia.ru,
# sitemap-catalog-brands.xml дал ~430 кандидатов-слагов; каждый проверен вручную через
# отдельный запрос к его странице (не механический transliteration слага — на живом примере
# ("miratorg" -> "мираторг" вместо "Мираторг") подтвердилось, что регистр по слагу ненадёжен).
# ~290 слагов оказались нерабочими ссылками (сайт откатывался на общий каталог без фильтра
# по бренду — не считаются подтверждёнными брендами). Из подтверждённых вручную удалены явные
# дубли/шум перед посевом: гомоглиф-дубль "Сheeseberry" (кириллическая С), два варианта одного
# бренда под разными слагами ("Диннер сервис"/"Dinner Service", "VAL'DEpro"/"Valde'Pro"),
# избыточное "Мясное производство РЕМИТ" рядом с "Ремит", юрлицо "ИП Поляков А.Н." (не бренд).
# Тот же idempotent-паттерн update_or_create по normalized_name, что и в core/0011_seed_base_units —
# безопасно повторно прогонять, не создаёт дублей поверх уже существующих Brand записей
# (например, введённых вручную поставщиками до этой миграции).

from django.db import migrations

BRAND_NAMES = [
    "Agrobar",
    "Akiso",
    "Alfakher",
    "Alti",
    "AstraFry",
    "Bagerstat",
    "Bagerstat Foodservice",
    "Barilla",
    "Barinoff",
    "Baruto",
    "Best Fries",
    "Bonfesto",
    "Bonfrit",
    "Cheeseberry",
    "Delphi",
    "Dinner Service",
    "Dolce Rosa",
    "Dunkan",
    "Dziugas",
    "Döckel",
    "Elita",
    "Esoro",
    "Feast",
    "Fine Food",
    "Fish & More",
    "FriendChef",
    "Fry Me",
    "Fumiko",
    "GREMILux",
    "Genso",
    "Got2eat",
    "Gourmand S.A.",
    "Green Heaven",
    "Hansey",
    "Heinz",
    "Hikari",
    "Hochland",
    "Jions",
    "Kandylas",
    "Kaneshiro",
    "Kekeshi",
    "Keysun",
    "Kotanyi",
    "La Vida",
    "Latteria",
    "Manna Food",
    "Mikado",
    "Mission",
    "Mission Professional",
    "Mr. Chu",
    "Namura",
    "Navafish Moscow",
    "OFI Caviar",
    "Olfood",
    "Oliveto",
    "Padam",
    "Panamania",
    "Pechagin Professional",
    "Pikador Pro",
    "PittaEllas",
    "Praohom",
    "Resfood",
    "Risma",
    "Sahar",
    "Sango",
    "Schönfeld",
    "Sea Star",
    "Shinaki",
    "SpiceExpert",
    "Stockson",
    "Sunny Gold",
    "SuperFresh",
    "TAMAKI Pro",
    "Tabuko",
    "Tamaki",
    "ToDoFood",
    "UEF",
    "Uniburger",
    "Valde'Pro",
    "Vegafry",
    "We Fry",
    "Zealandia",
    "Альгерд",
    "Боярин",
    "Ваша Выгода!",
    "Гурман",
    "Деликатес",
    "Деликатесы Севера",
    "Десертные Истории",
    "Домашний ресторатор",
    "Донская кухня",
    "Дымов",
    "Заготпром",
    "Знаменский",
    "Казанские Деликатесы",
    "Коломенский эксперт",
    "Ламбвестон",
    "М2",
    "МДМ",
    "МПС",
    "Маки Плюс",
    "Мидори",
    "Милетто",
    "Мираторг",
    "Молочная Речка",
    "МясоРезка",
    "Националь",
    "Нева Фуд",
    "Овощной край",
    "Панинское Плюс",
    "Петмол",
    "Пиканта",
    "Питер Фрост",
    "Промпоставка-М",
    "Пян-се",
    "Рамай",
    "Ремит",
    "Савушкин",
    "Санта Бремор",
    "Сантус",
    "Серволюкс",
    "СиВид",
    "Сычевские Фермы",
    "Сэн Сой",
    "ТАТ",
    "ТД Бенье",
    "Токио",
    "Умами",
    "Фабрика мороженого Престиж",
    "Фрост-А",
    "Черкизово",
    "Юкмп",
]


def seed_brands(apps, schema_editor):
    Brand = apps.get_model("catalog", "Brand")
    for name in BRAND_NAMES:
        Brand.objects.update_or_create(
            normalized_name=name.lower(),
            defaults={"canonical_name": name, "status": "active"},
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0006_alter_venuesupplierproduct_supplier_product_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_brands, noop_reverse),
    ]
