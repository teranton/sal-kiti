import logging
import uuid

import requests
from django.conf import settings
from django.core.cache import cache
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import extend_schema
from oauthlib.oauth2 import OAuth2Error
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from results.connectors.suomisport import Suomisport
from results.models.athletes import Athlete

logger = logging.getLogger(__name__)


class SuomisportRegistrationPermission(BasePermission):
    """
    Allows access to superusers and to members of the group named in settings
    SUOMISPORT["REGISTRATIONS_GROUP"] (default "suomisport_registrations").

    Each scoring program gets its own user in this group and uses that user's API token.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        group = _settings().get("REGISTRATIONS_GROUP", "suomisport_registrations")
        return user.groups.filter(name=group).exists()


class SuomisportRegistrationThrottle(UserRateThrottle):
    scope = "suomisport_registrations"

    def get_rate(self):
        return _settings().get("REGISTRATIONS_THROTTLE_RATE", "30/min")


def _settings():
    return getattr(settings, "SUOMISPORT", None) or {}


def _birth_year(user):
    birth_date = user.get("birthDate") or ""
    return int(birth_date[:4]) if birth_date[:4].isdigit() else None


def _organization(athlete):
    if not athlete or not athlete.organization:
        return None
    organization = athlete.organization
    return {
        "id": organization.id,
        "name": organization.name,
        "abbreviation": organization.abbreviation,
        "sport_id": organization.sport_id,
    }


def _registration(registration, athletes):
    """
    Return only the fields a scoring program needs. Contact details, addresses, social security numbers,
    guardians and exact birth dates from Suomisport are never passed on.
    """
    user = registration.get("user") or {}
    sport_id = str(user.get("sportId") or "")
    athlete = athletes.get(sport_id)
    return {
        "id": registration.get("id"),
        "modification_time": registration.get("modificationTime"),
        "payment_status": registration.get("paymentStatus"),
        "paid_amount_in_cents": registration.get("paidAmountInCents"),
        "active_discount_type": registration.get("activeDiscountType"),
        "registration_info": registration.get("registrationInfo") or "",
        "sport_id": sport_id,
        "first_name": Suomisport._display_first_name(user),
        "last_name": Suomisport._capitalize_name(user.get("lastName") or ""),
        "gender": Suomisport._parse_gender(user.get("gender")),
        "birth_year": _birth_year(user),
        "sport_parameters": [
            {"sport": parameter.get("sportName"), "name": parameter.get("name"), "value": parameter.get("value")}
            for parameter in user.get("sportParameters") or []
        ],
        "athlete_id": athlete.id if athlete else None,
        "organization": _organization(athlete),
    }


def get_connector():
    return Suomisport()


def fetch_event_registrations(event_uuid):
    """
    Fetch event and its registrations from Suomisport and combine them with Kiti athlete information.

    :param event_uuid: Suomisport event UUID
    :return: response data or None if the event is not found
    """
    connector = get_connector()
    event = connector.get_event(event_uuid)
    if not event or not event.get("id"):
        return None
    registrations = connector.get_event_registrations(event.get("id"))
    sport_ids = {str((r.get("user") or {}).get("sportId") or "") for r in registrations} - {""}
    athletes = {
        athlete.sport_id: athlete
        for athlete in Athlete.objects.filter(sport_id__in=sport_ids).select_related("organization")
    }
    return {
        "event": {
            "id": event.get("id"),
            "uuid": event.get("uuid") or str(event_uuid),
            "name": event.get("name"),
            "organizer_id": event.get("organizerId"),
            "organizer_name": event.get("organizerName"),
            "start": event.get("startDateTime"),
            "end": event.get("endDateTime"),
            "attendee_count": event.get("currentAttendeeCount"),
            "max_attendee_count": event.get("maxAttendeeCount"),
        },
        "registrations": [_registration(registration, athletes) for registration in registrations],
    }


@extend_schema(
    responses={
        200: {
            "type": "object",
            "properties": {
                "event": {"type": "object"},
                "registrations": {"type": "array", "items": {"type": "object"}},
            },
        }
    },
)
@never_cache
@api_view()
@permission_classes([SuomisportRegistrationPermission])
@throttle_classes([SuomisportRegistrationThrottle])
def suomisport_event_registrations(request, event_uuid):
    """
    Registrations of a Suomisport event, for scoring programs.

    Fetched from Suomisport with Kiti's API connection and cached for SUOMISPORT["REGISTRATIONS_CACHE_SECONDS"]
    (default 120). Club information comes from Kiti's athlete register.
    """
    try:
        event_uuid = str(uuid.UUID(event_uuid))
    except ValueError:
        return Response({"detail": "Event not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _settings().get("BASE_URL"):
        return Response(
            {"detail": "Suomisport connection is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    cache_key = "suomisport_registrations_%s" % event_uuid
    data = cache.get(cache_key)
    if data is None:
        try:
            data = fetch_event_registrations(event_uuid)
        except requests.RequestException as error:
            logger.warning("Suomisport registration fetch failed for event %s: %s", event_uuid, error)
            response = getattr(error, "response", None)
            if response is not None and response.status_code in (401, 403):
                return Response(
                    {"detail": "Suomisport denied access to the event registrations."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            return Response({"detail": "Suomisport is not available."}, status=status.HTTP_502_BAD_GATEWAY)
        except OAuth2Error as error:
            logger.warning("Suomisport connection failed for event %s: %s", event_uuid, error)
            return Response({"detail": "Suomisport is not available."}, status=status.HTTP_502_BAD_GATEWAY)
        if data is None:
            return Response({"detail": "Event not found."}, status=status.HTTP_404_NOT_FOUND)
        cache.set(cache_key, data, _settings().get("REGISTRATIONS_CACHE_SECONDS", 120))
    logger.info(
        "Suomisport registrations for event %s served to %s (%d rows)",
        event_uuid,
        request.user.username,
        len(data["registrations"]),
    )
    return Response(data)
