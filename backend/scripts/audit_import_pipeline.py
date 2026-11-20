"""Isolated executions of repository functions; no DB, HTTP, Celery, or LLM calls."""
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace as NS
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from contextlib import nullcontext

ROOT = Path(__file__).resolve().parents[1]

def extract(path, names, namespace, cls=None):
    tree = ast.parse((ROOT / path).read_text())
    nodes = tree.body if cls is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body
    chosen = [n for n in nodes if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in chosen:
        node.decorator_list = []
    exec(compile(ast.Module(body=chosen, type_ignores=[]), str(ROOT / path), 'exec'), namespace)
    return namespace

class ValidationError(Exception):
    pass

class Query:
    def __init__(self, records):
        self.records = records
    def filter(self, **kwargs):
        return self
    def select_related(self, *args):
        return self
    def all(self):
        return self.records
    def first(self):
        return next(iter(self.records), None)
    def __iter__(self):
        return iter(self.records)

def saved(**kwargs):
    return NS(**kwargs, save=lambda **kw: None)

def response(data, status=200):
    return NS(data=data, status_code=status)

actions = NS(REVIEW='review', CREATE_NEW='create_new', UPDATE_EXISTING='update_existing', SKIP='skip', NEW='new', REPLACE='replace')
base = dict(CatalogImportItemAction=actions, CatalogImportDomain=NS(MASTER='master', VENUE='venue'),
            CatalogImportSessionStatus=NS(OPEN='open', COMMITTED='committed'), ValidationError=ValidationError,
            Response=response, transaction=NS(atomic=nullcontext), status=NS(HTTP_409_CONFLICT=409),
            _require_ready=lambda session: None)
post = extract('api/v1/catalog/views.py', ['post'], base.copy(), 'CatalogImportSessionDetailView')['post']
results = []

def record(name, observed, **details):
    results.append(dict(check=name, observed=observed, **details))

# Master service expects UUID objects; persisted JSON has UUID strings.
uid = uuid4()
master_globals = dict(MasterProduct=NS(objects=Query([])), Unit=NS(objects=Query([NS(id=uid)])),
                      EntityStatus=NS(ACTIVE='active'), _decisions_error=lambda message: ValidationError(message))
apply_master = extract('api/v1/catalog/services.py', ['apply_master_product_import'], master_globals)['apply_master_product_import']
try:
    apply_master(owner=NS(id=uuid4()), decisions=[dict(action='create_new', raw_name='Мука', unit_id=str(uid))])
except ValidationError as exc:
    assert 'единицы измерения' in str(exc)
    record('master_session_unit_uuid', 'existing active UUID string rejected', error=str(exc))
else:
    raise AssertionError('Expected active UUID string rejection')

# Session commit ignores normalized values and returns raw service objects.
item = saved(id=uuid4(), raw_values=dict(raw_name='Original'), normalized_values=dict(raw_name='Edited'),
             action='create_new', target_id=None, target_expected_updated_at=None)
session = saved(id=uuid4(), owner=NS(id=uuid4()), domain='master', status='open', items=Query([item]))
calls = []
post.__globals__['apply_master_product_import'] = lambda **kw: calls.append(kw) or [NS(raw_name='Original')]
result = post(NS(_session=lambda request, sid: session), NS(user=session.owner), session.id)
assert calls[0]['decisions'][0]['raw_name'] == 'Original'
assert session.status == 'committed'
record('normalized_values_ignored', 'commit uses Original, ignores Edited')
try:
    json.dumps(result.data)
except TypeError as exc:
    record('commit_response_rendering', 'not JSON serializable after committed state', error=str(exc))
else:
    raise AssertionError('Expected SimpleNamespace serialization failure')

# A conflicting row plus a valid row closes the entire session.
bad = saved(id=uuid4(), raw_values=dict(raw_name='Conflict'), action='update_existing', target_id=uuid4(),
            target_expected_updated_at='old', validation_errors=[])
good = saved(id=uuid4(), raw_values=dict(raw_name='Good'), action='create_new', target_id=None,
             target_expected_updated_at=None)
session = saved(id=uuid4(), owner=NS(id=uuid4()), domain='master', status='open', items=Query([bad, good]))
post.__globals__['MasterProduct'] = NS(objects=Query([NS(updated_at='new')]))
post.__globals__['apply_master_product_import'] = lambda **kw: []
result = post(NS(_session=lambda request, sid: session), NS(user=session.owner), session.id)
assert bad.action == 'review' and session.status == 'committed'
record('partial_conflict_closes_session', 'review row remains in committed session', response=result.data)

# A JSONField permits non-object raw_values, causing a server-side TypeError.
item.raw_values = []
session = saved(id=uuid4(), owner=NS(id=uuid4()), domain='master', status='open', items=Query([item]))
try:
    post(NS(_session=lambda request, sid: session), NS(user=session.owner), session.id)
except TypeError as exc:
    record('untyped_raw_values', 'non-object raw_values accepted by JSONField crashes commit', error=str(exc))

# Extended parser truncates excess rows without an error.
extended_ns = dict(extract_rows_from_file=lambda *a, **k: [NS(row_number=i, cells={'text': f'Product {i}'}) for i in range(1, 5)],
                   _FILE_IMPORT_HEADER_ALIASES=(), _FIELD_ALIASES={'name': {'name'}}, ValidationError=ValidationError,
                   ParsedCatalogFile=lambda **kw: NS(**kw))
parser = extract('api/v1/catalog/importers.py', ['_parse_extended_catalog_file'], extended_ns)['_parse_extended_catalog_file']
result = parser(NS(name='list.txt'), max_rows=2)
assert len(result.rows) == 2
record('extended_parser_silent_truncation', '4 extracted rows -> 2 accepted rows without error')

# Supplier parsing discards plain TXT/OCR text rows, although extraction supports them.
parsing_ns = dict(_max_import_rows=lambda: 12000, _EXACT_HEADER_ALIASES={},
                  _COLUMN_ALIASES={'name': ['название', 'наименование', 'номенклатура', 'товар']})
extract('api/v1/suppliers/product_import_parsing.py', ['_field_for_header_key', '_rows_from_raw_import_rows'], parsing_ns)
result = parsing_ns['_rows_from_raw_import_rows']([NS(row_number=1, cells={'text': 'Молоко 1 л'})])
assert result == []
record('supplier_plain_text_discarded', 'TXT/OCR text cell produces zero product rows')

# Price parsing loses ordinary Russian money formatting; negative values satisfy readiness.
numbers_ns = dict(Decimal=Decimal, InvalidOperation=InvalidOperation)
decimal_fn = extract('api/v1/suppliers/product_import_selectors.py', ['_to_decimal'], numbers_ns)['_to_decimal']
assert decimal_fn('1 200,50') is None
record('supplier_formatted_price_lost', '1 200,50 -> None')
required = ('country_of_origin', 'unit_id', 'price', 'delivery_multiple', 'net_value', 'net_unit_id', 'min_order_qty', 'availability')
ready_ns = dict(SupplierProductImportAction=actions, _REQUIRED_FIELDS_FOR_READY=required)
ready = extract('api/v1/suppliers/product_import_services.py', ['_is_item_ready'], ready_ns)['_is_item_ready']
negative_values = {field: Decimal('-1') for field in required}
negative_values.update(country_of_origin='Россия', unit_id=uuid4(), net_unit_id=uuid4(), availability='in_stock')
negative_item = NS(action='new', **negative_values)
assert ready(negative_item)
record('supplier_negative_numbers_ready', 'negative price, net, minimum and multiple satisfy readiness')

# SKU duplicate detection resets between processing batches.
product = NS(id=uuid4(), updated_at='version', supplier_name='Existing', unit_id=None)
sku_ns = dict(find_supplier_products_by_sku=lambda **kw: {'sku': product}, SupplierProductImportAction=actions)
sku_fn = extract('api/v1/suppliers/product_import_selectors.py', ['_apply_sku_matches'], sku_ns)['_apply_sku_matches']
batches = [[NS(supplier_sku='sku', action='review')], [NS(supplier_sku='sku', action='review')]]
for batch in batches:
    sku_fn(supplier=NS(id=uuid4()), items=batch)
assert all(batch[0].action == 'replace' for batch in batches)
record('sku_duplicate_across_batches', 'same SKU gets replace in both batches')

parsing_tree = ast.parse((ROOT / 'api/v1/suppliers/product_import_parsing.py').read_text())
for assignment in parsing_tree.body:
    if isinstance(assignment, ast.Assign) and assignment.targets[0].id in ('_COLUMN_ALIASES', '_EXACT_HEADER_ALIASES'):
        parsing_ns[assignment.targets[0].id] = ast.literal_eval(assignment.value)
assert parsing_ns['_field_for_header_key']('единица нетто') == 'unit_label'
record('supplier_net_unit_column_misclassified', 'Единица нетто is treated as sale unit, not net unit')

extended_ns['_FIELD_ALIASES'] = {'name': {'наименование товара'}, 'unit': {'ед. изм'}}
extended_ns['extract_rows_from_file'] = lambda *a, **kw: [NS(row_number=2, cells={'наименование товара': 'Мука'})]
try:
    parser(NS(name='price.docx'), max_rows=12000)
except ValidationError as exc:
    record('extended_name_header_dropped', 'Наименование товара in DOCX table produces no rows', error=str(exc))
else:
    raise AssertionError('Expected header-mapping failure')

extended_ns['_normalize_header'] = lambda value: ' '.join(re.sub(r'[^\w]+', ' ', str(value).casefold()).split())
extended_ns['extract_rows_from_file'] = lambda *a, **kw: [NS(row_number=2, cells={'наименование': 'Мука', 'ед. изм': 'кг'})]
result = parser(NS(name='price.docx'), max_rows=12000)
assert 'unit' not in result.rows[0]
record('extended_unit_header_dropped', 'Ед. изм in DOCX table loses unit')

tree = ast.parse((ROOT / 'catalog/models.py').read_text())
enum = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CatalogImportItemAction')
action_values = [n.value.elts[0].value for n in enum.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Tuple)]
assert 'replace' not in action_values
record('catalog_session_replace_unavailable', 'replace absent from session enum', allowed_actions=action_values)

# Unrelated PATCH refreshes the expected version even when target was not changed.
patch_ns = base.copy()
patch_item = saved(id=uuid4(), session_id=uuid4(), action='update_existing', target_id=uuid4(),
                   target_expected_updated_at='version-seen-at-preview', raw_values={}, normalized_values={},
                   field_sources={}, candidates=[], validation_errors=[])
patch_session = saved(id=patch_item.session_id, owner_id=uuid4(), status='open')
patch_item.session = patch_session
class PatchSerializer:
    def __init__(self, data):
        self.validated_data = data
    def is_valid(self, **kwargs):
        return True
patch_ns.update(CatalogImportItem=NS(objects=Query([])), get_object_or_404=lambda *a, **kw: patch_item,
                CatalogImportSessionItemPatchRequestSerializer=PatchSerializer,
                CatalogImportSessionItemPatchResponseSerializer=lambda data: NS(data=data),
                _import_target=lambda *args: NS(updated_at='version-after-concurrent-edit'))
patch_fn = extract('api/v1/catalog/views.py', ['patch'], patch_ns, 'CatalogImportSessionItemView')['patch']
patch_fn(NS(), NS(user=NS(id=patch_session.owner_id), data={'normalized_values': {'note': 'Unrelated edit'}}),
         patch_item.session_id, patch_item.id)
assert patch_item.target_expected_updated_at == 'version-after-concurrent-edit'
record('optimistic_version_refreshed_on_unrelated_patch', 'old target version silently replaced on unrelated edit')

# Real DRF runtime, when available. Installations are confined to /tmp.
try:
    from django.conf import settings
    if not settings.configured:
        settings.configure(USE_I18N=False, REST_FRAMEWORK={})
    from rest_framework import serializers
    from rest_framework.renderers import JSONRenderer
    from rest_framework.views import exception_handler
    from django.core.exceptions import ValidationError as DjangoValidationError
except ImportError:
    record('drf_runtime', 'not installed; ORM/HTTP-stub checks only')
else:
    try:
        JSONRenderer().render({'status': 'committed', 'rows': [NS(raw_name='Мука')]})
    except TypeError as exc:
        record('real_drf_commit_rendering', 'DRF JSONRenderer rejects service result', error=str(exc))
    assert exception_handler(DjangoValidationError('Уже импортированную строку нельзя изменить.'), {}) is None
    record('real_drf_django_error', 'default exception handler does not handle Django ValidationError')
    source = ast.parse((ROOT / 'api/v1/catalog/serializers.py').read_text())
    catalog_patch = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'CatalogImportSessionItemPatchRequestSerializer')
    actions.choices = [(value, value) for value in action_values]
    globals_patch = dict(serializers=serializers, CatalogImportItemAction=actions)
    exec(compile(ast.Module(body=[catalog_patch], type_ignores=[]), 'catalog/serializers.py', 'exec'), globals_patch)
    patch_serializer = globals_patch[catalog_patch.name]
    for payload in ({'raw_values': []}, {'raw_values': {'raw_name': ''}}, {'normalized_values': {'raw_name': 'Edited'}}):
        serializer = patch_serializer(data=payload)
        assert serializer.is_valid()
    record('real_drf_untyped_patch', 'actual catalog serializer accepts array, empty name, normalized_values')
    serializer = patch_serializer(data={'action': 'replace'})
    assert not serializer.is_valid()
    record('real_drf_replace_rejected', 'actual catalog serializer rejects action=replace')

print(json.dumps(dict(mode='isolated AST execution with ORM/HTTP stubs; no end-to-end database tests',
                      checks=results), ensure_ascii=False, indent=2, default=str))
