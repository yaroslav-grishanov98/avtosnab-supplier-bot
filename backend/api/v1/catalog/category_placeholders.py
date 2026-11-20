"""Картинки-заглушки по виду товара для прототипа каталога (2026-09-29).

Пользователю нужно показать, как каталог выглядит с фото, а у товаров стенда фото почти
нет. Решение пользователя (вариант А): не грузить копии в каждый SupplierProduct, а
отдавать в `image_url` товара без своего фото картинку его вида товара — 60 векторных
иллюстраций в `catalog/static/catalog/category-placeholders/<file>.svg` (плоский стиль,
нарисованы кодом, без текста и брендов, 1–2 КБ каждая), раздаются как обычная статика
(WhiteNoise, collectstatic при деплое). Настоящее фото поставщика всегда в
приоритете; ответ помечает заглушку `image_is_placeholder=true`. ВРЕМЕННО, для демо —
убрать перед реальным запуском (см. docs/wiki, backlog).

Вид товара определяется по названиям уровней дерева категорий, а не по UUID: так одна
и та же таблица работает на любом стенде и для категорий, созданных позже. На стенде
часть дерева записана перевёрнутой («вид / продукты питания / группа») — это учтено.
Товару без категории или с видом, для которого нет своей картинки, отдаётся общая
картинка «продукты» (GENERIC_FILE, 2026-09-30: на стенде 1313 из 7107 товаров без
категории). Нет и её файла — заглушка не отдаётся вовсе."""

from functools import lru_cache

from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static

PLACEHOLDER_DIR = "catalog/category-placeholders"
PLACEHOLDER_EXT = "svg"
GENERIC_FILE = "generic-grocery"

# вид товара (нормализованное название нижнего уровня) -> имя файла без расширения
CONCEPT_FILES = {
    "аджика,горчица,хрен": "mustard-adjika-horseradish",
    "азиатская кухня": "asian-foods",
    "вафли": "wafers",
    "вермуты и аперитивы": "vermouth",
    "винные напитки": "wine-drinks",
    "граппа, кальвадос и абсент": "grappa-calvados-absinthe",
    "другое (охрана и доступ)": "security-access",
    "замороженные (овощи и грибы)": "frozen-vegetables",
    "замороженные тесто и выпечка": "frozen-dough-pastry",
    "замороженные фрукты": "frozen-fruit",
    "зерновой кофе": "coffee-beans",
    "йогурты": "yogurt",
    "кексы,рулеты": "cakes-rolls",
    "кетчупы": "ketchup",
    "консервированные (овощи и грибы)": "canned-vegetables",
    "консервы (рыба и морепродукты)": "canned-fish",
    "копчености,шпик,сало": "smoked-meats",
    "крупы и бобовые": "grains-legumes",
    "листовой (чай)": "loose-leaf-tea",
    "майонез": "mayonnaise",
    "макаронные изделия": "pasta",
    "маргарин": "margarine",
    "мед,джемы": "honey-jam",
    "молоко": "milk",
    "молотый кофе": "ground-coffee",
    "молочные коктейли и напитки": "milkshakes",
    "мука и все для выпечки": "flour-baking",
    "оливковое масло": "olive-oil",
    "пакетированный (чай)": "tea-bags",
    "питьевая вода": "drinking-water",
    "плавленые сыры": "processed-cheese",
    "пожарные рукава": "fire-hose",
    "полутвердые сыры": "semi-hard-cheese",
    "полуфабрикаты": "meat-semi-finished",
    "пресервы": "preserved-fish",
    "продукты быстрого приготовления": "instant-food",
    "прохладительные напитки": "soft-drinks",
    "пшено": "millet",
    "рассольные сыры,брынза,фета": "brine-cheese",
    "растворимый кофе": "instant-coffee",
    "растительное масло": "vegetable-oil",
    "сахар,соль": "sugar-salt",
    "сгущенное молоко и какао": "condensed-milk",
    "сиропы,топпинги": "syrups-toppings",
    "сливки": "cream",
    "сливочное масло": "butter",
    "сметана": "sour-cream",
    "соки,морсы,нектары": "juices",
    "сосиски,сардельки": "sausages",
    "соусы": "sauces",
    "специи,приправы": "spices",
    "спреды": "spreads",
    "сыры с белой плесенью": "white-mold-cheese",
    "сыры с голубой плесенью": "blue-cheese",
    "твердые сыры": "hard-cheese",
    "творог": "cottage-cheese",
    "творожные и сливочные сыры": "cream-cheese",
    "уксус,маринады": "vinegar-marinades",
    "форель филе": "trout-fillet",
    "хлеб,батон,лаваш": "bread",
}
CONCEPT_ALIASES = {
    "азиатские продукты": "азиатская кухня",
    "горчица": "аджика,горчица,хрен",
    "камамбер": "сыры с белой плесенью",
    "сливки 33%": "сливки",
    "соусы и приправы (бакалея)": "соусы",
}
# Названия, которые сами по себе ничего не значат — уточняются родительским уровнем.
AMBIGUOUS_LEAVES = {
    "замороженные",
    "консервированные",
    "консервы",
    "листовой",
    "пакетированный",
    "другое",
    "соусы и приправы",
}
_FOOD_ROOT = "продукты питания"


def _norm(value):
    return (value or "").strip().lower().replace("ё", "е")


def concept_key(category):
    parent = category.parent
    group = parent.parent if parent is not None else None
    name, parent_name, group_name = _norm(category.name), _norm(parent and parent.name), _norm(group and group.name)
    if parent_name == _FOOD_ROOT and group_name and group_name != _FOOD_ROOT:
        leaf, context = group_name, name
    else:
        leaf, context = name, parent_name
    key = f"{leaf} ({context})" if leaf in AMBIGUOUS_LEAVES else leaf
    return CONCEPT_ALIASES.get(key, key)


@lru_cache(maxsize=None)
def _static_path_if_exists(filename):
    path = f"{PLACEHOLDER_DIR}/{filename}.{PLACEHOLDER_EXT}"
    return path if finders.find(path) else None


def category_placeholder_path(category):
    """Путь картинки-заглушки в статике: вид товара категории, иначе общая картинка, иначе None."""
    filename = CONCEPT_FILES.get(concept_key(category)) if category is not None else None
    return (filename and _static_path_if_exists(filename)) or _static_path_if_exists(GENERIC_FILE)


def placeholder_url(static_path, request=None):
    """Ссылка на статику, пригодная для <img src> на другом домене/протоколе.

    CATALOG_PLACEHOLDER_BASE_URL — явный базовый адрес, если задан. Иначе ссылка без схемы
    (`//host/static/...`): за TLS-прокси без SECURE_PROXY_SSL_HEADER request.build_absolute_uri
    дал бы http:// и браузер заблокировал бы картинку на https-странице (mixed content)."""
    try:
        url = static(static_path)
    except ValueError:  # нет записи в манифесте collectstatic
        url = "/" + f"{settings.STATIC_URL.strip('/')}/{static_path}"
    base = getattr(settings, "CATALOG_PLACEHOLDER_BASE_URL", "")
    if base:
        return f"{base.rstrip('/')}{url}"
    if request is not None:
        return f"//{request.get_host()}{url}"
    return url
