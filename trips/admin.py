from django.contrib import admin
from .models import TripPlan

@admin.register(TripPlan)
class TripPlanAdmin(admin.ModelAdmin):
    list_display = ('destination', 'session_key', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('destination', 'session_key')
    readonly_fields = ('ai_draft', 'thread_id', 'session_key')