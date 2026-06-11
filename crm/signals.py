"""Object-permission wiring via django-guardian + stage bootstrapping.

* On Deal save, ensure the owner holds `view_deal` and `change_deal` on that
  row. If ownership changes, the previous owner's perms are revoked.
* On Deal *create*, seed an initial "Application" stage so every deal has a
  current_stage.
"""

from __future__ import annotations

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from guardian.shortcuts import assign_perm, remove_perm

from .models import Deal, Stage

_PREVIOUS_OWNER_ATTR = "_previous_owner_id"


@receiver(pre_save, sender=Deal)
def _capture_previous_owner(sender, instance: Deal, **kwargs):
    if instance.pk:
        previous = Deal.objects.filter(pk=instance.pk).values_list("owner_id", flat=True).first()
        setattr(instance, _PREVIOUS_OWNER_ATTR, previous)
    else:
        setattr(instance, _PREVIOUS_OWNER_ATTR, None)


@receiver(post_save, sender=Deal)
def _sync_owner_object_perms(sender, instance: Deal, created: bool, **kwargs):
    previous_owner_id = getattr(instance, _PREVIOUS_OWNER_ATTR, None)

    if not created and previous_owner_id and previous_owner_id != instance.owner_id:
        from django.contrib.auth import get_user_model
        prev = get_user_model().objects.filter(pk=previous_owner_id).first()
        if prev is not None:
            remove_perm("view_deal", prev, instance)
            remove_perm("change_deal", prev, instance)

    if instance.owner_id:
        assign_perm("view_deal", instance.owner, instance)
        assign_perm("change_deal", instance.owner, instance)


@receiver(post_save, sender=Deal)
def _bootstrap_initial_stage(sender, instance: Deal, created: bool, **kwargs):
    """Seed an 'Application' stage event the first time a deal is created."""
    if created:
        Stage.objects.create(
            deal=instance,
            name=Stage.Name.APPLICATION,
            organisation=instance.organisation,
            set_by=instance.owner,
        )
