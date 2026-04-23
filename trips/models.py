from django.db import models

class TripPlan(models.Model):
    class Status(models.TextChoices):
        COLLECTING = 'COLLECTING', 'Collecting Info'
        DRAFT = 'DRAFT', 'Draft Ready'
        APPROVED = 'APPROVED', 'Approved'
        FAILED = 'FAILED', 'Failed'

    session_key = models.CharField(max_length=40, db_index=True)
    thread_id = models.CharField(max_length=100, unique=True, blank=True, null=True)
    destination = models.CharField(max_length=200, blank=True)
    budget = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    days = models.PositiveIntegerField(null=True, blank=True)
    interests = models.TextField(blank=True)
    travel_type = models.CharField(max_length=50, blank=True)   # e.g., solo, family
    ai_draft = models.TextField(blank=True)
    final_itinerary = models.TextField(blank=True)
    user_feedback = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.COLLECTING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)