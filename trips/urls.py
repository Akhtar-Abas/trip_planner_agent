from django.urls import path
from . import views

urlpatterns = [
    path('', views.chat_page, name='chat'),               
    path('api/start/', views.api_start, name='api_start'),   
    path('api/send/', views.api_send, name='api_send'),      
]