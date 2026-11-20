"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from api.v1.qa_views import qa_share_access_page
from api.v1.views import (
    cabinet_by_supplier_view,
    cabinet_update_proxy,
    cabinet_view,
    price_list_crud,
    supplier_add_view,
    supplier_product_archive_proxy,
    supplier_product_image_upload,
    supplier_product_price_history,
    supplier_product_update_proxy,
    supplier_products_list_proxy,
    supplier_products_page,
    supplier_upload_apply_proxy,
    supplier_upload_file_proxy,
    supplier_upload_status_proxy,
    supplier_upload_text_proxy,
    suppliers_list_view,
    team_access_page,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("api.v1.urls", namespace="v1")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/schema/swagger-ui/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # Aliases matching the URLs already deployed/bookmarked on the dev server
    path("api/swagger/", SpectacularSwaggerView.as_view(url_name="schema")),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema")),
    path("prices/", price_list_crud, name="price-list-crud"),
    path("supplier/<uuid:supplier_id>/", supplier_products_page, name="supplier-products"),
    # Server-side proxy endpoints for the supplier products page (token stays on server)
    path(
        "supplier/<uuid:supplier_id>/api/products/", supplier_products_list_proxy, name="supplier-products-list-proxy"
    ),
    path(
        "supplier/<uuid:supplier_id>/api/products/<uuid:product_id>/",
        supplier_product_update_proxy,
        name="supplier-product-update-proxy",
    ),
    path(
        "supplier/<uuid:supplier_id>/api/products/<uuid:product_id>/archive/",
        supplier_product_archive_proxy,
        name="supplier-product-archive-proxy",
    ),
    path(
        "supplier/<uuid:supplier_id>/api/uploads/file/", supplier_upload_file_proxy, name="supplier-upload-file-proxy"
    ),
    path(
        "supplier/<uuid:supplier_id>/api/uploads/text/", supplier_upload_text_proxy, name="supplier-upload-text-proxy"
    ),
    path(
        "supplier/<uuid:supplier_id>/api/uploads/<uuid:upload_id>/apply/",
        supplier_upload_apply_proxy,
        name="supplier-upload-apply-proxy",
    ),
    path(
        "supplier/<uuid:supplier_id>/api/uploads/<uuid:upload_id>/status/",
        supplier_upload_status_proxy,
        name="supplier-upload-status-proxy",
    ),
    path(
        "supplier/<uuid:supplier_id>/api/products/<uuid:product_id>/prices/",
        supplier_product_price_history,
        name="supplier-product-price-history",
    ),
    path(
        "supplier/<uuid:supplier_id>/api/images/", supplier_product_image_upload, name="supplier-product-image-upload"
    ),
    path("suppliers/", suppliers_list_view, name="suppliers-list"),
    path("suppliers/add/", supplier_add_view, name="supplier-add"),
    path("cabinet/", cabinet_view, name="cabinet"),
    path("cabinet/supplier/<uuid:supplier_id>/", cabinet_by_supplier_view, name="cabinet-by-supplier"),
    path("cabinet/supplier/<uuid:supplier_id>/update/", cabinet_update_proxy, name="cabinet-update-proxy"),
    # QA-only harness for the 2026-08-01 share-access work — 404s outside DEBUG (see api/v1/qa_views.py).
    path("qa/share-access/", qa_share_access_page, name="qa-share-access"),
    # Standalone tool: team list + invite + revoke for a company owner (2026-08-02, works on any deploy).
    path("team-access/", team_access_page, name="team-access"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
