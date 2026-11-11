from django.urls import path

from . import api, views

app_name = "dashboard"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("crypto/", views.crypto, name="crypto"),
    path("forex/", views.forex, name="forex"),
    path("naia/", views.naia, name="naia"),
    path("nctpi/", views.nctpi, name="nctpi"),
    path("risk/", views.risk, name="risk"),
    path("performance/", views.performance, name="performance"),
    path("api/summary/", api.summary, name="api-summary"),
    path("api/equity/", api.equity, name="api-equity"),
    path("api/positions/", api.positions, name="api-positions"),
    path("api/funnel/", api.funnel, name="api-funnel"),
]
