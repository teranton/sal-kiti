from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token

from results.middleware import current_user
from results.tests.factories.athletes import AthleteFactory
from results.tests.factories.organizations import OrganizationFactory

EVENT_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"

EVENT = {
    "id": 4321,
    "uuid": EVENT_UUID,
    "name": "Compak SM",
    "organizerId": 12,
    "organizerName": "Ampumaseura",
    "startDateTime": "2027-06-05T09:00:00Z",
    "endDateTime": "2027-06-06T17:00:00Z",
    "currentAttendeeCount": 2,
    "maxAttendeeCount": 120,
}

REGISTRATIONS = [
    {
        "id": 1,
        "modificationTime": "2027-05-01T10:00:00.000Z",
        "eventId": 4321,
        "user": {
            "id": 11,
            "sportId": 123456789,
            "firstName": "MATTI JUHANI",
            "lastName": "meikäläinen",
            "birthDate": "1970-03-01",
            "gender": "Male",
            "email": "matti@example.org",
            "phone": "+358401234567",
            "address": {"streetAddress": "Katu 1", "postCode": "00100", "postOffice": "Helsinki"},
            "socialSecurityNumber": "010370-123A",
            "guardians": [{"firstName": "Guard", "lastName": "Ian", "email": "g@example.org"}],
            "sportParameters": [{"sportName": "Haulikko", "name": "Sarja", "value": "M60"}],
            "isEventInstructor": False,
        },
        "registrationInfo": "Vasenkätinen",
        "paymentStatus": "PAID",
        "paidAmountInCents": 6000,
        "activeDiscountType": "NONE",
    },
    {
        "id": 2,
        "modificationTime": "2027-05-02T10:00:00.000Z",
        "eventId": 4321,
        "user": {
            "id": 12,
            "sportId": 987654321,
            "firstName": "Maija",
            "nickname": "Maju",
            "lastName": "Virtanen",
            "gender": "Female",
            "isEventInstructor": False,
        },
        "paymentStatus": "WAITING_PAYMENT_CONFIRMATION",
        "paidAmountInCents": 0,
        "activeDiscountType": "CODE",
    },
]


def connector_mock(event=EVENT, registrations=REGISTRATIONS, error=None):
    connector = MagicMock()
    connector.get_event.return_value = event
    if error:
        connector.get_event_registrations.side_effect = error
    else:
        connector.get_event_registrations.return_value = registrations
    return connector


class SuomisportRegistrationsTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("suomisport-event-registrations", kwargs={"event_uuid": EVENT_UUID})
        self.user = User.objects.create(username="scoringprogram")
        group = Group.objects.create(name="suomisport_registrations")
        self.user.groups.add(group)
        self.token, _ = Token.objects.get_or_create(user=self.user)

    def tearDown(self):
        # The test client runs CurrentUserMiddleware, which keeps the request user for the change log.
        current_user._local.user = None

    def _get(self, token=None):
        headers = {"HTTP_AUTHORIZATION": "Token " + token.key} if token else {}
        return self.client.get(self.url, **headers)

    def test_anonymous_is_denied(self):
        response = self._get()
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    @patch("results.views.suomisport.get_connector")
    def test_user_without_group_is_denied(self, mock_connector):
        other = User.objects.create(username="other")
        token, _ = Token.objects.get_or_create(user=other)
        response = self._get(token)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_connector.assert_not_called()

    @patch("results.views.suomisport.get_connector")
    def test_registrations(self, mock_connector):
        mock_connector.return_value = connector_mock()
        organization = OrganizationFactory.create(name="Ampumaseura ry", abbreviation="AS", sport_id="555")
        athlete = AthleteFactory.create(sport_id="123456789", organization=organization)

        response = self._get(self.token)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["event"]["id"], 4321)
        self.assertEqual(data["event"]["name"], "Compak SM")
        mock_connector.return_value.get_event_registrations.assert_called_once_with(4321)
        first, second = data["registrations"]
        self.assertEqual(first["sport_id"], "123456789")
        self.assertEqual(first["first_name"], "Matti")
        self.assertEqual(first["last_name"], "Meikäläinen")
        self.assertEqual(first["gender"], "M")
        self.assertEqual(first["birth_year"], 1970)
        self.assertEqual(first["payment_status"], "PAID")
        self.assertEqual(first["registration_info"], "Vasenkätinen")
        self.assertEqual(first["sport_parameters"], [{"sport": "Haulikko", "name": "Sarja", "value": "M60"}])
        self.assertEqual(first["athlete_id"], athlete.id)
        self.assertEqual(first["organization"]["abbreviation"], "AS")
        self.assertEqual(second["first_name"], "Maju")
        self.assertEqual(second["active_discount_type"], "CODE")
        self.assertIsNone(second["birth_year"])
        self.assertIsNone(second["organization"])

    @patch("results.views.suomisport.get_connector")
    def test_personal_data_is_not_passed_on(self, mock_connector):
        mock_connector.return_value = connector_mock()
        content = self._get(self.token).content.decode()
        for value in ("matti@example.org", "+358401234567", "Katu 1", "010370-123A", "1970-03-01", "Guard"):
            self.assertNotIn(value, content)

    @patch("results.views.suomisport.get_connector")
    def test_response_is_cached(self, mock_connector):
        mock_connector.return_value = connector_mock()
        self.assertEqual(self._get(self.token).status_code, status.HTTP_200_OK)
        self.assertEqual(self._get(self.token).status_code, status.HTTP_200_OK)
        self.assertEqual(mock_connector.call_count, 1)

    @patch("results.views.suomisport.get_connector")
    def test_unknown_event(self, mock_connector):
        mock_connector.return_value = connector_mock(event=None)
        response = self._get(self.token)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("results.views.suomisport.get_connector")
    def test_suomisport_error(self, mock_connector):
        error_response = MagicMock(status_code=403)
        mock_connector.return_value = connector_mock(error=requests.HTTPError(response=error_response))
        response = self._get(self.token)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIsNone(cache.get("suomisport_registrations_%s" % EVENT_UUID))

    @patch("results.views.suomisport.get_connector")
    def test_superuser_is_allowed(self, mock_connector):
        mock_connector.return_value = connector_mock()
        admin = User.objects.create(username="admin", is_superuser=True)
        token, _ = Token.objects.get_or_create(user=admin)
        self.assertEqual(self._get(token).status_code, status.HTTP_200_OK)
