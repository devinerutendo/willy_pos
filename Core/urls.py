"""
URL configuration for Core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
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
from django.contrib import admin
from django.urls import path, include
from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from django.conf import settings
from django.conf.urls.static import static

schema_view = get_schema_view(
    openapi.Info(
        title="Inventory Management API",
        default_version='v1',
        description="""
        Inventory Management System API documentation.
        
        ## Features
        - **User Management**: Manage users with different roles (Manager/Sales)
        - **Inventory**: Categories, Products, Variants, Stock, Transactions
        - **Sales**: Quotations, Sales, and related operations
        
        ## Authentication
        All endpoints require authentication via session or basic auth.
        """,
        contact=openapi.Contact(email="contact@example.com"),
        license=openapi.License(name="MIT License"),
    ),
    public=True,
    permission_classes=[],
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/core/', include('Core.api_urls')),
    path('api/accounts/', include('Accounts.urls')),
    path('api/licensing/', include('Licensing.urls')),
    path('api/inventory/', include('Inventory.urls')),
    path('api/sales/', include('Sales.urls')),
    path('api/sync/', include('Sync.urls')),
    path('api/notifications/', include('Notifications.urls')),
    
    # Swagger/OpenAPI Documentation
    path('api/docs/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui-alt'),
    path('api/docs/swagger.json', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    path('swagger.json', schema_view.without_ui(cache_timeout=0), name='schema-json-alt'),
    path('api/docs/redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc-alt'),
]

# Serve collected static files from STATIC_ROOT even when DEBUG=False.
# This helps admin CSS/JS load in local/test environments when DEBUG is disabled.
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)