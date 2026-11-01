"""Wipe the database and load the demo data: three tenants of very different sizes, 1,000
pending calls and, for the largest, a history of finished calls per contact."""

import random
from datetime import time, timedelta
from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from calls.models import (
    Call,
    CallResult,
    Campaign,
    CampaignOwnership,
    Contact,
    ScriptStep,
    Tenant,
)

# name, pending calls, finished calls per contact
TENANTS = [('acme', 50, (0, 0)), ('beta', 250, (0, 0)), ('gamma', 700, (15, 20))]
CONTRACTS: dict[str, dict[str, Any]] = {
    'acme': {
        'max_attempts': 3,
        'retry_delay': timedelta(minutes=30),
        'calling_opens': time(9),
        'calling_closes': time(21),
    },
    'beta': {
        'max_attempts': 2,
        'retry_delay': timedelta(hours=2),
        'calling_opens': time(10),
        'calling_closes': time(20),
    },
    'gamma': {},  # the defaults: no retries
}
STEPS = [
    'Greet the contact and confirm you are speaking with the account holder.',
    'Explain the reason for the call and present the pending balance.',
    'Offer the payment options and agree on the next step.',
]
OUTCOMES = ['completed', 'completed', 'user_hangup', 'timeout', 'error']


class Command(BaseCommand):
    help = __doc__

    def handle(self, *args: Any, **options: Any) -> None:
        rng = random.Random(42)
        now = timezone.now()
        tables = ', '.join(
            model._meta.db_table for model in apps.get_app_config('calls').models.values()
        )
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f'TRUNCATE {tables} RESTART IDENTITY CASCADE')
            for name, pending, history in TENANTS:
                tenant = Tenant.objects.create(
                    name=name, 
                    system_prompt=f'You call on behalf of {name}. Be brief and polite.',
                    **CONTRACTS[name]
                )
                campaign = Campaign.objects.create(name=f'{name} collections')
                CampaignOwnership.objects.create(campaign=campaign, tenant=tenant)
                ScriptStep.objects.bulk_create(
                    ScriptStep(campaign=campaign, order=i, text=text)
                    for i, text in enumerate(STEPS, start=1)
                )
                contacts = Contact.objects.bulk_create(
                    Contact(
                        name=f'{name} contact {i}', phone=f'+34{600_000_000 + rng.randrange(10**8)}'
                    )
                    for i in range(pending)
                )
                Call.objects.bulk_create(Call(campaign=campaign, contact=c) for c in contacts)

                finished, results = [], []
                for contact in contacts:
                    for day in range(rng.randint(*history)):
                        call = Call(
                            campaign=campaign,
                            contact=contact,
                            status=Call.Status.FINISHED,
                            created_at=now - timedelta(days=day + 1),
                        )
                        finished.append(call)
                        results.append(
                            CallResult(
                                call=call,
                                outcome=rng.choice(OUTCOMES),
                                duration_s=rng.uniform(20, 400),
                            )
                        )
                Call.objects.bulk_create(finished, batch_size=2000)
                CallResult.objects.bulk_create(results, batch_size=2000)

        self.stdout.write(
            f'Seeded {Call.objects.filter(status=Call.Status.PENDING).count()} pending calls.'
        )
