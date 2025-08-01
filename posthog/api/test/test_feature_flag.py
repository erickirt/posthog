from datetime import datetime, timedelta, UTC
import json
from typing import Optional
from unittest.mock import call, patch

from django.core.cache import cache
from django.db import connection
from django.db.utils import OperationalError
from django.test import TransactionTestCase
from django.test.client import RequestFactory
from django.utils.timezone import now
from freezegun.api import freeze_time
from parameterized import parameterized
from rest_framework import status

from posthog import redis
from posthog.api.cohort import get_cohort_actors_for_feature_flag
from posthog.api.feature_flag import FeatureFlagSerializer
from posthog.constants import AvailableFeature
from posthog.models import Experiment, FeatureFlag, GroupTypeMapping, User
from posthog.models.cohort import Cohort
from posthog.models.dashboard import Dashboard
from products.early_access_features.backend.models import EarlyAccessFeature
from posthog.models.feature_flag import (
    FeatureFlagDashboards,
    get_all_feature_flags,
    get_feature_flags_for_team_in_cache,
)
from posthog.models.feature_flag.feature_flag import FeatureFlagHashKeyOverride
from posthog.models.group.util import create_group
from posthog.models.organization import Organization
from posthog.models.person import Person
from posthog.models.personal_api_key import PersonalAPIKey, hash_key_value
from posthog.models.team.team import Team
from posthog.models.utils import generate_random_token_personal, generate_random_token_secret
from posthog.models.feature_flag.flag_status import (
    FeatureFlagStatus,
)
from posthog.test.base import (
    APIBaseTest,
    ClickhouseTestMixin,
    FuzzyInt,
    QueryMatchingTest,
    _create_person,
    flush_persons_and_events,
    snapshot_clickhouse_queries,
    snapshot_postgres_queries_context,
)
from posthog.test.db_context_capturing import capture_db_queries


class TestFeatureFlag(APIBaseTest, ClickhouseTestMixin):
    feature_flag: FeatureFlag = None  # type: ignore

    maxDiff = None

    def setUp(self):
        cache.clear()

        # delete all keys in redis
        r = redis.get_client()
        for key in r.scan_iter("*"):
            r.delete(key)
        return super().setUp()

    def test_cant_create_flag_with_more_than_max_values(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags",
            {
                "name": "Beta feature",
                "key": "beta-x",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 65,
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": [
                                        "1@gmail.com",
                                        "2@gmail.com",
                                        "3@gmail.com",
                                        "4@gmail.com",
                                        "5@gmail.com",
                                        "6@gmail.com",
                                        "7@gmail.com",
                                        "8@gmail.com",
                                        "9@gmail.com",
                                        "10@gmail.com",
                                        "11@gmail.com",
                                        "12@gmail.com",
                                    ],
                                    "operator": "exact",
                                }
                            ],
                        }
                    ]
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "Property group expressions of type email cannot contain more than 10 values.",
                "attr": "filters",
            },
        )

    def test_cant_create_flag_with_duplicate_key(self):
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        count = FeatureFlag.objects.count()
        # Make sure the endpoint works with and without the trailing slash
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags",
            {"name": "Beta feature", "key": "red_button"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "type": "validation_error",
                "code": "unique",
                "detail": "There is already a feature flag with this key.",
                "attr": "key",
            },
        )
        self.assertEqual(FeatureFlag.objects.count(), count)

    @parameterized.expand(
        [
            ("foo?bar=baz",),
            ("foo/bar",),
            ("foo\\bar",),
            ("foo.bar",),
            ("foo bar",),
        ]
    )
    def test_cant_create_flag_with_key_with_invalid_characters(self, key):
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        count = FeatureFlag.objects.count()
        # Make sure the endpoint works with and without the trailing slash
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags",
            {"name": "Beta feature", "key": key},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "type": "validation_error",
                "code": "invalid_key",
                "detail": "Only letters, numbers, hyphens (-) & underscores (_) are allowed.",
                "attr": "key",
            },
        )
        self.assertEqual(FeatureFlag.objects.count(), count)

    def test_cant_create_flag_with_key_too_long(self):
        key = "a" * 400 + "b"
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        count = FeatureFlag.objects.count()
        # Make sure the endpoint works with and without the trailing slash
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags",
            {"name": "Beta feature", "key": key},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "type": "validation_error",
                "code": "max_length",
                "detail": "Ensure this field has no more than 400 characters.",
                "attr": "key",
            },
        )
        self.assertEqual(FeatureFlag.objects.count(), count)

    def test_cant_create_flag_with_invalid_filters(self):
        count = FeatureFlag.objects.count()

        invalid_operators = ["icontains", "regex", "not_icontains", "not_regex", "lt", "gt", "lte", "gte"]

        for operator in invalid_operators:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags",
                {
                    "name": "Beta feature",
                    "key": "beta-x",
                    "filters": {
                        "groups": [
                            {
                                "rollout_percentage": 65,
                                "properties": [
                                    {
                                        "key": "email",
                                        "type": "person",
                                        "value": ["@posthog.com"],
                                        "operator": operator,
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(
                response.json(),
                {
                    "type": "validation_error",
                    "code": "invalid_value",
                    "detail": f"Invalid value for operator {operator}: ['@posthog.com']",
                    "attr": "filters",
                },
            )

        self.assertEqual(FeatureFlag.objects.count(), count)

        # Test that a string value is still acceptable
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags",
            {
                "name": "Beta feature",
                "key": "beta-x",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 65,
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": '["@posthog.com"]',  # fine as long as a string
                                    "operator": "not_regex",
                                }
                            ],
                        }
                    ]
                },
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_cant_update_flag_with_duplicate_key(self):
        existing_flag = FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        another_feature_flag = FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=50,
            name="some feature",
            key="some-feature",
            created_by=self.user,
        )
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{another_feature_flag.pk}",
            {"name": "Beta feature", "key": "red_button"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "type": "validation_error",
                "code": "unique",
                "detail": "There is already a feature flag with this key.",
                "attr": "key",
            },
        )
        another_feature_flag.refresh_from_db()
        self.assertEqual(another_feature_flag.key, "some-feature")

        # Try updating the existing one
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{existing_flag.id}/",
            {"name": "Beta feature 3", "key": "red_button"},
        )
        self.assertEqual(response.status_code, 200)
        existing_flag.refresh_from_db()
        self.assertEqual(existing_flag.name, "Beta feature 3")

    def test_is_simple_flag(self):
        feature_flag = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {"groups": [{"rollout_percentage": 65}]},
            },
            format="json",
        ).json()
        self.assertTrue(feature_flag["is_simple_flag"])
        self.assertEqual(feature_flag["rollout_percentage"], 65)

    def test_is_not_simple_flag(self):
        feature_flag = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 65,
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "@posthog.com",
                                    "operator": "icontains",
                                }
                            ],
                        }
                    ]
                },
            },
            format="json",
        ).json()
        self.assertFalse(feature_flag["is_simple_flag"])

    @patch("posthog.api.feature_flag.report_user_action")
    def test_is_simple_flag_groups(self, mock_capture):
        feature_flag = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [{"rollout_percentage": 65}],
                },
            },
            format="json",
        ).json()
        self.assertFalse(feature_flag["is_simple_flag"])
        # Assert analytics are sent
        instance = FeatureFlag.objects.get(id=feature_flag["id"])
        mock_capture.assert_called_once_with(
            self.user,
            "feature flag created",
            {
                "groups_count": 1,
                "has_variants": False,
                "variants_count": 0,
                "has_rollout_percentage": True,
                "has_filters": False,
                "filter_count": 0,
                "created_at": instance.created_at,
                "aggregating_by_groups": True,
                "payload_count": 0,
                "creation_context": "feature_flags",
            },
        )

    @freeze_time("2021-08-25T22:09:14.252Z")
    @patch("posthog.api.feature_flag.report_user_action")
    def test_create_feature_flag(self, mock_capture):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {"groups": [{"rollout_percentage": 50}]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        flag_id = response.json()["id"]
        instance = FeatureFlag.objects.get(id=flag_id)
        self.assertEqual(instance.key, "alpha-feature")

        # Assert analytics are sent
        mock_capture.assert_called_once_with(
            self.user,
            "feature flag created",
            {
                "groups_count": 1,
                "has_variants": False,
                "variants_count": 0,
                "has_rollout_percentage": True,
                "has_filters": False,
                "filter_count": 0,
                "created_at": instance.created_at,
                "aggregating_by_groups": False,
                "payload_count": 0,
                "creation_context": "feature_flags",
            },
        )

        self.assert_feature_flag_activity(
            flag_id,
            [
                {
                    "user": {"first_name": "", "email": "user1@posthog.com"},
                    "activity": "created",
                    "created_at": "2021-08-25T22:09:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [],
                        "trigger": None,
                        "type": None,
                        "name": "alpha-feature",
                        "short_id": None,
                    },
                }
            ],
        )

        self.assertEqual(instance.created_by, self.user)

    @patch("posthog.api.feature_flag.report_user_action")
    def test_create_minimal_feature_flag(self, mock_capture):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {"key": "omega-feature"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["key"], "omega-feature")
        self.assertEqual(response.json()["name"], "")
        instance = FeatureFlag.objects.get(id=response.json()["id"])
        self.assertEqual(instance.key, "omega-feature")
        self.assertEqual(instance.name, "")

        # Assert analytics are sent
        mock_capture.assert_called_once_with(
            self.user,
            "feature flag created",
            {
                "groups_count": 1,  # 1 is always created by default
                "has_variants": False,
                "variants_count": 0,
                "has_rollout_percentage": False,
                "has_filters": False,
                "filter_count": 0,
                "created_at": instance.created_at,
                "aggregating_by_groups": False,
                "payload_count": 0,
                "creation_context": "feature_flags",
            },
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_create_feature_flag_with_analytics_dashboards(self, mock_capture):
        dashboard = Dashboard.objects.create(team=self.team, name="private dashboard", created_by=self.user)
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {"key": "feature-with-analytics-dashboards", "analytics_dashboards": [dashboard.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["key"], "feature-with-analytics-dashboards")
        self.assertEqual(len(response.json()["analytics_dashboards"]), 1)
        instance = FeatureFlag.objects.get(id=response.json()["id"])
        self.assertEqual(instance.key, "feature-with-analytics-dashboards")
        self.assertEqual(instance.analytics_dashboards.all()[0].id, dashboard.pk)

    @patch("posthog.api.feature_flag.report_user_action")
    def test_create_feature_flag_with_evaluation_runtime(self, mock_capture):
        # Test creating a feature flag with different evaluation_runtime values

        # Test with "server"
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {"key": "server-side-flag", "evaluation_runtime": "server"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["key"], "server-side-flag")
        self.assertEqual(response.json()["evaluation_runtime"], "server")
        instance = FeatureFlag.objects.get(id=response.json()["id"])
        self.assertEqual(instance.evaluation_runtime, "server")

        # Test with "client"
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {"key": "client-side-flag", "evaluation_runtime": "client"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["evaluation_runtime"], "client")

        # Test with "all"
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {"key": "all-flag", "evaluation_runtime": "all"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["evaluation_runtime"], "all")

        # Test default value (should be "all")
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {"key": "default-flag"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["evaluation_runtime"], "all")

    @patch("posthog.api.feature_flag.report_user_action")
    def test_update_feature_flag_evaluation_runtime(self, mock_capture):
        # Create a flag with default evaluation_runtime
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {"key": "flag-to-update"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        flag_id = response.json()["id"]
        self.assertEqual(response.json()["evaluation_runtime"], "all")

        # Update to "server"
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
            {"evaluation_runtime": "server"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["evaluation_runtime"], "server")

        # Verify in database
        instance = FeatureFlag.objects.get(id=flag_id)
        self.assertEqual(instance.evaluation_runtime, "server")

    @patch("posthog.api.feature_flag.report_user_action")
    def test_create_multivariate_feature_flag(self, mock_capture):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": None}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = FeatureFlag.objects.get(id=response.json()["id"])
        self.assertEqual(instance.key, "multivariate-feature")

        # Assert analytics are sent
        mock_capture.assert_called_once_with(
            self.user,
            "feature flag created",
            {
                "groups_count": 1,
                "has_variants": True,
                "variants_count": 3,
                "has_filters": False,
                "has_rollout_percentage": False,
                "filter_count": 0,
                "created_at": instance.created_at,
                "aggregating_by_groups": False,
                "payload_count": 0,
                "creation_context": "feature_flags",
            },
        )

    def test_cant_create_multivariate_feature_flag_with_variant_rollout_lt_100(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": None}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 0,
                            },
                        ]
                    },
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json().get("type"), "validation_error")
        self.assertEqual(
            response.json().get("detail"),
            "Invalid variant definitions: Variant rollout percentages must sum to 100.",
        )

    def test_cant_create_multivariate_feature_flag_with_variant_rollout_gt_100(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": None}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 50,
                            },
                        ]
                    },
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json().get("type"), "validation_error")
        self.assertEqual(
            response.json().get("detail"),
            "Invalid variant definitions: Variant rollout percentages must sum to 100.",
        )

    def test_cant_update_multivariate_feature_flag_with_variant_rollout_not_100(self):
        # Create initial flag
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": None}],
                    "multivariate": {
                        "variants": [
                            {"key": "control", "rollout_percentage": 50},
                            {"key": "test", "rollout_percentage": 50},
                        ]
                    },
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        feature_flag_id = response.json()["id"]

        # Try to update with invalid percentages
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{feature_flag_id}",
            {
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": None}],
                    "multivariate": {
                        "variants": [
                            {"key": "control", "rollout_percentage": 50},
                            {"key": "test", "rollout_percentage": 40},
                        ]
                    },
                }
            },
            format="json",
        )

        # Verify error response
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json().get("type"), "validation_error")
        self.assertEqual(
            response.json().get("detail"),
            "Invalid variant definitions: Variant rollout percentages must sum to 100.",
        )

        # Verify flag wasn't updated
        feature_flag = FeatureFlag.objects.get(id=feature_flag_id)
        self.assertEqual(feature_flag.filters["multivariate"]["variants"][0]["rollout_percentage"], 50)
        self.assertEqual(feature_flag.filters["multivariate"]["variants"][1]["rollout_percentage"], 50)

    def test_cant_create_feature_flag_without_key(self):
        count = FeatureFlag.objects.count()
        response = self.client.post(f"/api/projects/{self.team.id}/feature_flags/", format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "type": "validation_error",
                "code": "required",
                "detail": "This field is required.",
                "attr": "key",
            },
        )
        self.assertEqual(FeatureFlag.objects.count(), count)

    def test_cant_create_multivariate_feature_flag_with_invalid_variant_overrides(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": None,
                            "variant": "unknown-variant",
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json().get("type"), "validation_error")
        self.assertEqual(
            response.json().get("detail"),
            "Filters are not valid (variant override does not exist)",
        )

    def test_cant_update_multivariate_feature_flag_with_invalid_variant_overrides(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": None,
                            "variant": "second-variant",
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        feature_flag_id = response.json()["id"]

        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{feature_flag_id}",
            {
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": None,
                            "variant": "unknown-variant",
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json().get("type"), "validation_error")
        self.assertEqual(
            response.json().get("detail"),
            "Filters are not valid (variant override does not exist)",
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {
                    "name": "Updated name",
                    "filters": {
                        "groups": [
                            {
                                "rollout_percentage": 65,
                                "properties": [
                                    {
                                        "key": "email",
                                        "type": "person",
                                        "value": "@posthog.com",
                                        "operator": "icontains",
                                    }
                                ],
                            }
                        ]
                    },
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(response.json()["name"], "Updated name")
        self.assertEqual(response.json()["filters"]["groups"][0]["rollout_percentage"], 65)

        # Assert analytics are sent
        mock_capture.assert_called_with(
            self.user,
            "feature flag updated",
            {
                "groups_count": 1,
                "has_variants": False,
                "variants_count": 0,
                "has_rollout_percentage": True,
                "has_filters": True,
                "filter_count": 1,
                "created_at": datetime.fromisoformat("2021-08-25T22:09:14.252000+00:00"),
                "aggregating_by_groups": False,
                "payload_count": 0,
            },
        )

        self.assert_feature_flag_activity(
            flag_id,
            [
                {
                    "user": {
                        "first_name": self.user.first_name,
                        "email": self.user.email,
                    },
                    "activity": "updated",
                    "created_at": "2021-08-25T22:19:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [
                            {
                                "type": "FeatureFlag",
                                "action": "changed",
                                "field": "name",
                                "before": "original name",
                                "after": "Updated name",
                            },
                            {
                                "type": "FeatureFlag",
                                "action": "created",
                                "field": "filters",
                                "before": None,
                                "after": {
                                    "groups": [
                                        {
                                            "properties": [
                                                {
                                                    "key": "email",
                                                    "type": "person",
                                                    "value": "@posthog.com",
                                                    "operator": "icontains",
                                                }
                                            ],
                                            "rollout_percentage": 65,
                                        }
                                    ]
                                },
                            },
                            {"action": "changed", "after": 2, "before": 1, "field": "version", "type": "FeatureFlag"},
                        ],
                        "trigger": None,
                        "type": None,
                        "name": "a-feature-flag-that-is-updated",
                        "short_id": None,
                    },
                },
                {
                    "user": {
                        "first_name": self.user.first_name,
                        "email": self.user.email,
                    },
                    "activity": "created",
                    "created_at": "2021-08-25T22:09:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [],
                        "trigger": None,
                        "type": None,
                        "name": "a-feature-flag-that-is-updated",
                        "short_id": None,
                    },
                },
            ],
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_partial(self, mock_capture):
        # Test that we can update a feature flag with only some of the fields
        # And the unchanged fields are not updated
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {
                    "name": "original name",
                    "key": "a-feature-flag-that-is-updated",
                    "filters": {
                        "groups": [
                            {
                                "variant": None,
                                "properties": [
                                    {"key": "plan", "type": "person", "value": ["pro"], "operator": "exact"}
                                ],
                                "rollout_percentage": 100,
                            }
                        ],
                        "payloads": {},
                        "multivariate": None,
                    },
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {
                    "name": "Updated name",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(response.json()["name"], "Updated name")
        self.assertEqual(response.json()["filters"]["groups"][0]["rollout_percentage"], 100)

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_with_different_user(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            # Create flag with original user
            original_user = self.user
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            # Create and login as different user
            different_user = User.objects.create_and_join(self.organization, "different_user@posthog.com", None)
            self.client.force_login(different_user)
            self.assertNotEqual(original_user, different_user)

            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {"name": "Updated name"},
                format="json",
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Grab the feature flag and assert created_by is original user and updated_by is different user
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            self.assertEqual(feature_flag.created_by, original_user)
            self.assertEqual(feature_flag.last_modified_by, different_user)

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_fails_concurrency_check_when_version_outdated(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            # Create flag with original user: version 0
            original_user = self.user
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]
            original_version = response.json()["version"]
            self.assertEqual(original_version, 1)
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            self.assertEqual(feature_flag.version, 1)
            self.assertEqual(feature_flag.last_modified_by, original_user)

            frozen_datetime.tick(delta=timedelta(minutes=10))

            # Create and login as different user
            different_user = User.objects.create_and_join(self.organization, "different_user@posthog.com", None)
            self.client.force_login(different_user)
            self.assertNotEqual(original_user, different_user)

            # Successfully update the feature flag with the different user. This will increment the version
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {"name": "Updated name", "version": original_version},
                format="json",
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            updated_version = response.json()["version"]
            self.assertEqual(updated_version, 2)
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            self.assertEqual(feature_flag.version, 2)
            self.assertEqual(feature_flag.last_modified_by, different_user)

            self.client.force_login(original_user)

            # Original user tries to update the feature flag with the original version
            # This should fail because the version has been incremented and the user is
            # trying to update the name
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                data={
                    "name": "Another Updated name",
                    "version": original_version,
                    "original_flag": {
                        # Name has since been changed, leading to a conflict
                        "name": "original name",
                        "key": "a-feature-flag-that-is-updated",
                    },
                },
                format="json",
            )

            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(response.json().get("type"), "server_error")
            self.assertEqual(
                response.json().get("detail"),
                "The feature flag was updated by different_user@posthog.com since you started editing it. Please refresh and try again.",
            )

            # Grab the feature flag and assert created_by is original user and last_modified_by is different user
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            self.assertEqual(feature_flag.name, "Updated name")
            self.assertEqual(feature_flag.last_modified_by, different_user)

            # The different user refreshes and tries to update again
            self.client.force_login(different_user)
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                data={"name": "Another Updated name", "version": updated_version},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            self.assertEqual(feature_flag.name, "Another Updated name")
            self.assertEqual(feature_flag.last_modified_by, different_user)

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_does_not_fail_concurrency_check_when_changing_different_fields(self, mock_capture):
        # If another users saves changes, but my changes don't conflict with those changes,
        # then we should not fail the concurrency check
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            # Create flag with original user: version 0
            original_user = self.user
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {
                    "name": "original name",
                    "key": "a-feature-flag-that-is-updated",
                    "filters": {
                        "groups": [
                            {
                                "variant": None,
                                "properties": [
                                    {"key": "plan", "type": "person", "value": ["pro"], "operator": "exact"}
                                ],
                                "rollout_percentage": 100,
                            }
                        ],
                        "payloads": {},
                        "multivariate": None,
                    },
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]
            original_version = response.json()["version"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            # Create and login as different user
            different_user = User.objects.create_and_join(self.organization, "different_user@posthog.com", None)
            self.client.force_login(different_user)

            # Successfully update the feature flag with the different user. This will increment the version
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {"name": "Updated name", "version": original_version},
                format="json",
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.client.force_login(original_user)

            # Original user tries to update the feature flag with the original version
            # However, the user is changing a field that wasn't changed by the other user
            # This should succeed
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                data={
                    "name": "Updated name",
                    "filters": {
                        "groups": [
                            {
                                "variant": None,
                                "properties": [
                                    {"key": "plan", "type": "person", "value": ["pro"], "operator": "exact"}
                                ],
                                "rollout_percentage": 45,
                            }
                        ],
                        "payloads": {},
                        "multivariate": None,
                    },
                    "original_flag": {
                        "name": "original name",  # This is the same as the name (though not the current name)
                        "filters": {
                            "groups": [
                                {
                                    "variant": None,
                                    "properties": [
                                        {"key": "plan", "type": "person", "value": ["pro"], "operator": "exact"}
                                    ],
                                    "rollout_percentage": 100,
                                }
                            ],
                            "payloads": {},
                            "multivariate": None,
                        },
                    },
                    "version": original_version,
                },
                format="json",
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            self.assertEqual(feature_flag.name, "Updated name")
            self.assertEqual(feature_flag.last_modified_by, original_user)
            self.assertEqual(response.json()["filters"]["groups"][0]["rollout_percentage"], 45)

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_does_not_fail_when_version_not_in_request(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                data={"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                data={"name": "Updated name"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.json()["version"], 2)

            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                data={"name": "Yet another updated name"},
                format="json",
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.json()["version"], 3)
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            self.assertEqual(feature_flag.version, 3)
            self.assertEqual(feature_flag.name, "Yet another updated name")

    def test_remote_config_with_personal_api_key(self):
        FeatureFlag.objects.create(
            team=self.team,
            key="my-remote-config-flag",
            name="Remote Config Flag",
            active=True,
            filters={
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 100,
                    }
                ],
                "payloads": {"true": '{"test": true}'},
            },
            is_remote_configuration=True,
        )
        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        self.client.logout()

        response = self.client.get(
            f"/api/projects/{self.team.id}/feature_flags/my-remote-config-flag/remote_config",
            HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), '{"test": true}')

    def test_remote_config_with_project_secret_api_key(self):
        self.team.rotate_secret_token_and_save(user=self.user, is_impersonated_session=False)
        FeatureFlag.objects.create(
            team=self.team,
            key="my-remote-config-flag",
            name="Remote Config Flag",
            active=True,
            filters={
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 100,
                    }
                ],
                "payloads": {"true": '{"test": true}'},
            },
            is_remote_configuration=True,
        )
        self.client.logout()
        response = self.client.get(
            f"/api/projects/{self.team.id}/feature_flags/my-remote-config-flag/remote_config",
            HTTP_AUTHORIZATION=f"Bearer {self.team.secret_api_token}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), '{"test": true}')

    def test_remote_config_returns_not_found_for_unknown_flag(self):
        response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/nonexistent_key/remote_config")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_get_conflicting_changes(self):
        feature_flag = FeatureFlag.objects.create(
            team=self.team,
            key="my-flag",
            name="Beta feature",
            active=True,
            filters={"groups": [{"properties": [], "rollout_percentage": 50}]},
        )

        serializer = FeatureFlagSerializer(instance=feature_flag, context={"team_id": self.team.id})

        original_flag = {
            "key": "my-flag",
            "name": "Alpha feature",  # This has since been changed by another user
            "active": True,
            "filters": {"groups": [{"properties": [], "rollout_percentage": 50}]},
        }

        # Test 1: No conflicts when changing fields that haven't been changed by another user
        # The name is different from the current value, but the user is not trying to change it
        validated_data = {"active": False, "key": "my-flag-2", "name": "Alpha feature"}
        conflicts = serializer._get_conflicting_changes(feature_flag, validated_data, original_flag)
        self.assertEqual(conflicts, [])

        # Test 2: Detect conflict when changing a field that has been changed by another user
        feature_flag.active = False
        feature_flag.save()
        validated_data = {"name": "Gamma feature"}
        conflicts = serializer._get_conflicting_changes(feature_flag, validated_data, original_flag)
        self.assertEqual(conflicts, ["name"])

    def test_get_conflicting_changes_returns_empty_when_original_flag_is_none(self):
        feature_flag = FeatureFlag.objects.create(
            team=self.team,
            key="my-flag",
            name="Beta feature",
            active=True,
            filters={"groups": [{"properties": [], "rollout_percentage": 50}]},
        )

        serializer = FeatureFlagSerializer(instance=feature_flag, context={"team_id": self.team.id})

        original_flag = None

        # Should be conflict, but since original_flag is None, it will be ignored
        feature_flag.active = False
        feature_flag.save()
        validated_data = {"name": "Gamma feature"}
        conflicts = serializer._get_conflicting_changes(feature_flag, validated_data, original_flag)
        self.assertEqual(conflicts, [])

    def test_get_conflicting_changes_with_filter_changes(self):
        feature_flag = FeatureFlag.objects.create(
            team=self.team,
            key="my-flag",
            name="Beta feature",
            active=True,
            filters={
                # This has since been changed by another user
                "groups": [{"properties": [], "rollout_percentage": 45}]
            },
        )

        serializer = FeatureFlagSerializer(instance=feature_flag, context={"team_id": self.team.id})

        original_flag = {
            "key": "my-flag",
            # This has since been changed by another user
            "name": "Alpha feature",
            "active": True,
            "filters": {
                # This has since been changed by another user
                "groups": [{"properties": [], "rollout_percentage": 50}]
            },
        }

        # Test 1: No conflicts when changing fields that haven't been changed by another user
        # The name and fliters are different from the current value, but the user is not trying to change them
        validated_data = {
            "active": False,
            "key": "my-flag-2",
            "name": "Alpha feature",
            "filters": {"groups": [{"properties": [], "rollout_percentage": 50}]},
        }
        conflicts = serializer._get_conflicting_changes(feature_flag, validated_data, original_flag)
        self.assertEqual(conflicts, [])

        # Test 2: Detect conflict when changing a field that has been changed by another user
        feature_flag.active = False
        feature_flag.save()
        validated_data = {
            "name": "Gamma feature",
            "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
            "active": False,
            "key": "my-flag-2",
        }
        conflicts = serializer._get_conflicting_changes(feature_flag, validated_data, original_flag)
        self.assertEqual(conflicts, ["name", "filters"])

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_treats_null_version_as_zero(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                data={"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            feature_flag.version = None
            feature_flag.save()
            frozen_datetime.tick(delta=timedelta(minutes=10))

            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                data={"name": "Updated name", "version": 0},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.json()["version"], 1)
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            self.assertEqual(feature_flag.version, 1)
            self.assertEqual(feature_flag.name, "Updated name")

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_key(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            # Assert that the insights were created properly.
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            assert feature_flag.usage_dashboard is not None, "Usage dashboard was not created"
            insights = feature_flag.usage_dashboard.insights
            total_volume_insight = insights.get(name="Feature Flag Called Total Volume")
            self.assertEqual(
                total_volume_insight.description,
                "Shows the number of total calls made on feature flag with key: a-feature-flag-that-is-updated",
            )
            self.assertEqual(
                total_volume_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
                "a-feature-flag-that-is-updated",
            )
            unique_users_insight = insights.get(name="Feature Flag calls made by unique users per variant")
            self.assertEqual(
                unique_users_insight.description,
                "Shows the number of unique user calls made on feature flag per variant with key: a-feature-flag-that-is-updated",
            )
            self.assertEqual(
                unique_users_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
                "a-feature-flag-that-is-updated",
            )

            # Update the feature flag key
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {
                    "key": "a-new-feature-flag-key",
                    "filters": {
                        "groups": [
                            {
                                "rollout_percentage": 65,
                                "properties": [
                                    {
                                        "key": "email",
                                        "type": "person",
                                        "value": "@posthog.com",
                                        "operator": "icontains",
                                    }
                                ],
                            }
                        ]
                    },
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(response.json()["key"], "a-new-feature-flag-key")
        self.assertEqual(response.json()["filters"]["groups"][0]["rollout_percentage"], 65)

        # Assert analytics are sent
        mock_capture.assert_called_with(
            self.user,
            "feature flag updated",
            {
                "groups_count": 1,
                "has_variants": False,
                "variants_count": 0,
                "has_rollout_percentage": True,
                "has_filters": True,
                "filter_count": 1,
                "created_at": datetime.fromisoformat("2021-08-25T22:09:14.252000+00:00"),
                "aggregating_by_groups": False,
                "payload_count": 0,
            },
        )

        self.assert_feature_flag_activity(
            flag_id,
            [
                {
                    "user": {
                        "first_name": self.user.first_name,
                        "email": self.user.email,
                    },
                    "activity": "updated",
                    "created_at": "2021-08-25T22:19:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [
                            {
                                "type": "FeatureFlag",
                                "action": "changed",
                                "field": "key",
                                "before": "a-feature-flag-that-is-updated",
                                "after": "a-new-feature-flag-key",
                            },
                            {
                                "type": "FeatureFlag",
                                "action": "created",
                                "field": "filters",
                                "before": None,
                                "after": {
                                    "groups": [
                                        {
                                            "properties": [
                                                {
                                                    "key": "email",
                                                    "type": "person",
                                                    "value": "@posthog.com",
                                                    "operator": "icontains",
                                                }
                                            ],
                                            "rollout_percentage": 65,
                                        }
                                    ]
                                },
                            },
                            {
                                "type": "FeatureFlag",
                                "action": "changed",
                                "field": "version",
                                "before": 1,
                                "after": 2,
                            },
                        ],
                        "trigger": None,
                        "type": None,
                        "name": "a-new-feature-flag-key",
                        "short_id": None,
                    },
                },
                {
                    "user": {
                        "first_name": self.user.first_name,
                        "email": self.user.email,
                    },
                    "activity": "created",
                    "created_at": "2021-08-25T22:09:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [],
                        "trigger": None,
                        "type": None,
                        "name": "a-feature-flag-that-is-updated",
                        "short_id": None,
                    },
                },
            ],
        )

        feature_flag = FeatureFlag.objects.get(id=flag_id)
        assert feature_flag.usage_dashboard is not None, "Usage dashboard was not created"
        insights = feature_flag.usage_dashboard.insights
        total_volume_insight = insights.get(name="Feature Flag Called Total Volume")
        self.assertEqual(
            total_volume_insight.description,
            "Shows the number of total calls made on feature flag with key: a-new-feature-flag-key",
        )
        self.assertEqual(
            total_volume_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
            "a-new-feature-flag-key",
        )
        unique_users_insight = insights.get(name="Feature Flag calls made by unique users per variant")
        self.assertEqual(
            unique_users_insight.description,
            "Shows the number of unique user calls made on feature flag per variant with key: a-new-feature-flag-key",
        )
        self.assertEqual(
            unique_users_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
            "a-new-feature-flag-key",
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_key_does_not_update_insight_with_changed_description(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            # Assert that the insights were created properly.
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            assert feature_flag.usage_dashboard is not None, "Usage dashboard was not created"
            insights = feature_flag.usage_dashboard.insights
            total_volume_insight = insights.get(name="Feature Flag Called Total Volume")
            self.assertEqual(
                total_volume_insight.description,
                "Shows the number of total calls made on feature flag with key: a-feature-flag-that-is-updated",
            )
            self.assertEqual(
                total_volume_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
                "a-feature-flag-that-is-updated",
            )
            unique_users_insight = insights.get(name="Feature Flag calls made by unique users per variant")
            self.assertEqual(
                unique_users_insight.description,
                "Shows the number of unique user calls made on feature flag per variant with key: a-feature-flag-that-is-updated",
            )
            self.assertEqual(
                unique_users_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
                "a-feature-flag-that-is-updated",
            )
            total_volume_insight.name = "This is a changed description"
            total_volume_insight.save()

            # Update the feature flag key
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {
                    "key": "a-new-feature-flag-key",
                    "filters": {
                        "groups": [
                            {
                                "rollout_percentage": 65,
                                "properties": [
                                    {
                                        "key": "email",
                                        "type": "person",
                                        "value": "@posthog.com",
                                        "operator": "icontains",
                                    }
                                ],
                            }
                        ]
                    },
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Total volume insight should not be updated because we changed its description
        # unique users insight should still be updated
        feature_flag = FeatureFlag.objects.get(id=flag_id)
        assert feature_flag.usage_dashboard is not None, "Usage dashboard was not created"
        insights = feature_flag.usage_dashboard.insights
        self.assertIsNone(insights.filter(name="Feature Flag Called Total Volume").first())
        total_volume_insight = insights.get(name="This is a changed description")
        self.assertEqual(
            total_volume_insight.description,
            "Shows the number of total calls made on feature flag with key: a-feature-flag-that-is-updated",
        )
        self.assertEqual(
            total_volume_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
            "a-feature-flag-that-is-updated",
        )
        unique_users_insight = insights.get(name="Feature Flag calls made by unique users per variant")
        self.assertEqual(
            unique_users_insight.description,
            "Shows the number of unique user calls made on feature flag per variant with key: a-new-feature-flag-key",
        )
        self.assertEqual(
            unique_users_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
            "a-new-feature-flag-key",
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_key_does_not_update_insight_with_changed_filter(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            # Assert that the insights were created properly.
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            assert feature_flag.usage_dashboard is not None, "Usage dashboard was not created"
            insights = feature_flag.usage_dashboard.insights
            total_volume_insight = insights.get(name="Feature Flag Called Total Volume")
            self.assertEqual(
                total_volume_insight.description,
                "Shows the number of total calls made on feature flag with key: a-feature-flag-that-is-updated",
            )
            self.assertEqual(
                total_volume_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
                "a-feature-flag-that-is-updated",
            )
            unique_users_insight = insights.get(name="Feature Flag calls made by unique users per variant")
            self.assertEqual(
                unique_users_insight.description,
                "Shows the number of unique user calls made on feature flag per variant with key: a-feature-flag-that-is-updated",
            )
            self.assertEqual(
                unique_users_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
                "a-feature-flag-that-is-updated",
            )
            total_volume_insight.query["source"]["properties"]["values"][0]["values"][0]["value"] = (
                "something_unexpected"
            )
            total_volume_insight.save()

            # Update the feature flag key
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {
                    "key": "a-new-feature-flag-key",
                    "filters": {
                        "groups": [
                            {
                                "rollout_percentage": 65,
                                "properties": [
                                    {
                                        "key": "email",
                                        "type": "person",
                                        "value": "@posthog.com",
                                        "operator": "icontains",
                                    }
                                ],
                            }
                        ]
                    },
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Total volume insight should not be updated because we changed its description
        # unique users insight should still be updated
        feature_flag = FeatureFlag.objects.get(id=flag_id)
        assert feature_flag.usage_dashboard is not None, "Usage dashboard was not created"
        insights = feature_flag.usage_dashboard.insights
        total_volume_insight = insights.get(name="Feature Flag Called Total Volume")
        self.assertEqual(
            total_volume_insight.description,
            "Shows the number of total calls made on feature flag with key: a-feature-flag-that-is-updated",
        )
        self.assertEqual(
            total_volume_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
            "something_unexpected",
        )
        unique_users_insight = insights.get(name="Feature Flag calls made by unique users per variant")
        self.assertEqual(
            unique_users_insight.description,
            "Shows the number of unique user calls made on feature flag per variant with key: a-new-feature-flag-key",
        )
        self.assertEqual(
            unique_users_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
            "a-new-feature-flag-key",
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_key_does_not_update_insight_with_removed_filter(self, mock_capture):
        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "original name", "key": "a-feature-flag-that-is-updated"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            flag_id = response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            # Assert that the insights were created properly.
            feature_flag = FeatureFlag.objects.get(id=flag_id)
            assert feature_flag.usage_dashboard is not None, "Usage dashboard was not created"
            insights = feature_flag.usage_dashboard.insights
            total_volume_insight = insights.get(name="Feature Flag Called Total Volume")
            self.assertEqual(
                total_volume_insight.description,
                "Shows the number of total calls made on feature flag with key: a-feature-flag-that-is-updated",
            )
            self.assertEqual(
                total_volume_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
                "a-feature-flag-that-is-updated",
            )
            unique_users_insight = insights.get(name="Feature Flag calls made by unique users per variant")
            self.assertEqual(
                unique_users_insight.description,
                "Shows the number of unique user calls made on feature flag per variant with key: a-feature-flag-that-is-updated",
            )
            self.assertEqual(
                unique_users_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
                "a-feature-flag-that-is-updated",
            )
            # clear the values from total_volume_insight.query["source"]["properties"]["values"]
            total_volume_insight.query["source"]["properties"]["values"] = []
            total_volume_insight.save()

            # Update the feature flag key
            response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {
                    "key": "a-new-feature-flag-key",
                    "filters": {
                        "groups": [
                            {
                                "rollout_percentage": 65,
                                "properties": [
                                    {
                                        "key": "email",
                                        "type": "person",
                                        "value": "@posthog.com",
                                        "operator": "icontains",
                                    }
                                ],
                            }
                        ]
                    },
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Total volume insight should not be updated because we changed its description
        # unique users insight should still be updated
        feature_flag = FeatureFlag.objects.get(id=flag_id)
        assert feature_flag.usage_dashboard is not None, "Usage dashboard was not created"
        insights = feature_flag.usage_dashboard.insights
        total_volume_insight = insights.get(name="Feature Flag Called Total Volume")
        self.assertEqual(
            total_volume_insight.description,
            "Shows the number of total calls made on feature flag with key: a-feature-flag-that-is-updated",
        )
        self.assertEqual(
            total_volume_insight.query["source"]["properties"]["values"],
            [],
        )
        unique_users_insight = insights.get(name="Feature Flag calls made by unique users per variant")
        self.assertEqual(
            unique_users_insight.description,
            "Shows the number of unique user calls made on feature flag per variant with key: a-new-feature-flag-key",
        )
        self.assertEqual(
            unique_users_insight.query["source"]["properties"]["values"][0]["values"][0]["value"],
            "a-new-feature-flag-key",
        )

    def test_hard_deleting_feature_flag_is_forbidden(self):
        new_user = User.objects.create_and_join(self.organization, "new_annotations@posthog.com", None)

        instance = FeatureFlag.objects.create(team=self.team, created_by=self.user, key="potato")
        self.client.force_login(new_user)

        response = self.client.delete(f"/api/projects/{self.team.id}/feature_flags/{instance.pk}/")

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(FeatureFlag.objects.filter(pk=instance.pk).exists())

    def test_get_feature_flag_activity(self):
        new_user = User.objects.create_and_join(
            organization=self.organization,
            email="person_acting_and_then_viewing_activity@posthog.com",
            password=None,
            first_name="Potato",
        )
        self.client.force_login(new_user)

        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            create_response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "feature flag with activity", "key": "feature_with_activity"},
            )

            self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
            flag_id = create_response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            update_response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {
                    "name": "feature flag with activity",
                    "filters": {"groups": [{"properties": [], "rollout_percentage": 74}]},
                },
                format="json",
            )

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)

        self.assert_feature_flag_activity(
            flag_id,
            [
                {
                    "user": {
                        "first_name": new_user.first_name,
                        "email": new_user.email,
                    },
                    "activity": "updated",
                    "created_at": "2021-08-25T22:19:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [
                            {
                                "type": "FeatureFlag",
                                "action": "created",
                                "field": "filters",
                                "before": None,
                                "after": {"groups": [{"properties": [], "rollout_percentage": 74}]},
                            },
                            {"action": "changed", "after": 2, "before": 1, "field": "version", "type": "FeatureFlag"},
                        ],
                        "trigger": None,
                        "type": None,
                        "name": "feature_with_activity",
                        "short_id": None,
                    },
                },
                {
                    "user": {
                        "first_name": new_user.first_name,
                        "email": new_user.email,
                    },
                    "activity": "created",
                    "created_at": "2021-08-25T22:09:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [],
                        "trigger": None,
                        "type": None,
                        "name": "feature_with_activity",
                        "short_id": None,
                    },
                },
            ],
        )

    def test_get_feature_flag_activity_for_all_flags(self):
        new_user = User.objects.create_and_join(
            organization=self.organization,
            email="person_acting_and_then_viewing_activity@posthog.com",
            password=None,
            first_name="Potato",
        )
        self.client.force_login(new_user)

        with freeze_time("2021-08-25T22:09:14.252Z") as frozen_datetime:
            create_response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "feature flag with activity", "key": "feature_with_activity"},
            )

            self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
            flag_id = create_response.json()["id"]

            frozen_datetime.tick(delta=timedelta(minutes=10))

            update_response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {
                    "name": "feature flag with activity",
                    "filters": {"groups": [{"properties": [], "rollout_percentage": 74}]},
                },
                format="json",
            )
            self.assertEqual(update_response.status_code, status.HTTP_200_OK)

            frozen_datetime.tick(delta=timedelta(minutes=10))

            second_create_response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": "a second feature flag", "key": "flag-two"},
            )

            self.assertEqual(second_create_response.status_code, status.HTTP_201_CREATED)
            second_flag_id = second_create_response.json()["id"]

        self.assert_feature_flag_activity(
            flag_id=None,
            expected=[
                {
                    "user": {
                        "first_name": new_user.first_name,
                        "email": new_user.email,
                    },
                    "activity": "created",
                    "created_at": "2021-08-25T22:29:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(second_flag_id),
                    "detail": {
                        "changes": [],
                        "trigger": None,
                        "type": None,
                        "name": "flag-two",
                        "short_id": None,
                    },
                },
                {
                    "user": {
                        "first_name": new_user.first_name,
                        "email": new_user.email,
                    },
                    "activity": "updated",
                    "created_at": "2021-08-25T22:19:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [
                            {
                                "type": "FeatureFlag",
                                "action": "created",
                                "field": "filters",
                                "before": None,
                                "after": {"groups": [{"properties": [], "rollout_percentage": 74}]},
                            },
                            {"action": "changed", "after": 2, "before": 1, "field": "version", "type": "FeatureFlag"},
                        ],
                        "trigger": None,
                        "type": None,
                        "name": "feature_with_activity",
                        "short_id": None,
                    },
                },
                {
                    "user": {
                        "first_name": new_user.first_name,
                        "email": new_user.email,
                    },
                    "activity": "created",
                    "created_at": "2021-08-25T22:09:14.252000Z",
                    "scope": "FeatureFlag",
                    "item_id": str(flag_id),
                    "detail": {
                        "changes": [],
                        "trigger": None,
                        "type": None,
                        "name": "feature_with_activity",
                        "short_id": None,
                    },
                },
            ],
        )

    def test_length_of_feature_flag_activity_does_not_change_number_of_db_queries(self):
        new_user = User.objects.create_and_join(
            organization=self.organization,
            email="person_acting_and_then_viewing_activity@posthog.com",
            password=None,
            first_name="Potato",
        )
        self.client.force_login(new_user)

        # create the flag
        create_response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {"name": "feature flag with activity", "key": "feature_with_activity"},
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        flag_id = create_response.json()["id"]

        # get the activity and capture number of queries made
        with capture_db_queries() as first_read_context:
            self._get_feature_flag_activity(flag_id)

        if isinstance(first_read_context.final_queries, int) and isinstance(first_read_context.initial_queries, int):
            first_activity_read_query_count = first_read_context.final_queries - first_read_context.initial_queries
        else:
            raise AssertionError("must be able to read query numbers from first activity log query")

        # update the flag
        update_response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
            {
                "name": "feature flag with activity",
                "filters": {"groups": [{"properties": [], "rollout_percentage": 74}]},
            },
            format="json",
        )
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)

        # get the activity and capture number of queries made
        with capture_db_queries() as second_read_context:
            self._get_feature_flag_activity(flag_id)

        if isinstance(second_read_context.final_queries, int) and isinstance(second_read_context.initial_queries, int):
            second_activity_read_query_count = second_read_context.final_queries - second_read_context.initial_queries
        else:
            raise AssertionError("must be able to read query numbers from second activity log query")

        self.assertEqual(first_activity_read_query_count, second_activity_read_query_count)

    def test_get_feature_flag_activity_only_from_own_team(self):
        # two users in two teams
        _, org_one_team, org_one_user = User.objects.bootstrap(
            organization_name="Org 1", email="org1@posthog.com", password=None
        )

        _, org_two_team, org_two_user = User.objects.bootstrap(
            organization_name="Org 2", email="org2@posthog.com", password=None
        )

        # two flags in team 1
        self.client.force_login(org_one_user)
        team_one_flag_one = self._create_flag_with_properties(
            name="team-1-flag-1", team_id=org_one_team.id, properties=[]
        ).json()["id"]
        team_one_flag_two = self._create_flag_with_properties(
            name="team-1-flag-2", team_id=org_one_team.id, properties=[]
        ).json()["id"]

        # two flags in team 2
        self.client.force_login(org_two_user)
        team_two_flag_one = self._create_flag_with_properties(
            name="team-2-flag-1", team_id=org_two_team.id, properties=[]
        ).json()["id"]
        team_two_flag_two = self._create_flag_with_properties(
            name="team-2-flag-2", team_id=org_two_team.id, properties=[]
        ).json()["id"]

        # user in org 1 gets activity
        self.client.force_login(org_one_user)
        self._get_feature_flag_activity(
            flag_id=team_one_flag_one,
            team_id=org_one_team.id,
            expected_status=status.HTTP_200_OK,
        )
        self._get_feature_flag_activity(
            flag_id=team_one_flag_two,
            team_id=org_one_team.id,
            expected_status=status.HTTP_200_OK,
        )
        self._get_feature_flag_activity(
            flag_id=team_two_flag_one,
            team_id=org_one_team.id,
            expected_status=status.HTTP_404_NOT_FOUND,
        )
        self._get_feature_flag_activity(
            flag_id=team_two_flag_two,
            team_id=org_one_team.id,
            expected_status=status.HTTP_404_NOT_FOUND,
        )

        # user in org 2 gets activity
        self.client.force_login(org_two_user)
        self._get_feature_flag_activity(
            flag_id=team_one_flag_two,
            team_id=org_two_team.id,
            expected_status=status.HTTP_404_NOT_FOUND,
        )
        self._get_feature_flag_activity(
            flag_id=team_one_flag_two,
            team_id=org_two_team.id,
            expected_status=status.HTTP_404_NOT_FOUND,
        )
        self._get_feature_flag_activity(
            flag_id=team_two_flag_one,
            team_id=org_two_team.id,
            expected_status=status.HTTP_200_OK,
        )
        self._get_feature_flag_activity(
            flag_id=team_two_flag_two,
            team_id=org_two_team.id,
            expected_status=status.HTTP_200_OK,
        )

    def test_paging_all_feature_flag_activity(self):
        for x in range(15):
            create_response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                {"name": f"feature flag {x}", "key": f"{x}"},
            )
            self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)

        # check the first page of data
        url = f"/api/projects/{self.team.id}/feature_flags/activity"
        first_page_response = self.client.get(url)
        self.assertEqual(first_page_response.status_code, status.HTTP_200_OK)
        first_page_json = first_page_response.json()

        self.assertEqual(
            [log_item["detail"]["name"] for log_item in first_page_json["results"]],
            ["14", "13", "12", "11", "10", "9", "8", "7", "6", "5"],
        )
        self.assertEqual(
            first_page_json["next"],
            f"http://testserver/api/projects/{self.team.id}/feature_flags/activity?page=2&limit=10",
        )
        self.assertEqual(first_page_json["previous"], None)

        # check the second page of data
        second_page_response = self.client.get(first_page_json["next"])
        self.assertEqual(second_page_response.status_code, status.HTTP_200_OK)
        second_page_json = second_page_response.json()

        self.assertEqual(
            [log_item["detail"]["name"] for log_item in second_page_json["results"]],
            ["4", "3", "2", "1", "0"],
        )
        self.assertEqual(second_page_json["next"], None)
        self.assertEqual(
            second_page_json["previous"],
            f"http://testserver/api/projects/{self.team.id}/feature_flags/activity?page=1&limit=10",
        )

    def test_paging_specific_feature_flag_activity(self):
        create_response = self.client.post(f"/api/projects/{self.team.id}/feature_flags/", {"name": "ff", "key": "0"})
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        flag_id = create_response.json()["id"]

        for x in range(1, 15):
            update_response = self.client.patch(
                f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
                {"key": str(x)},
                format="json",
            )
            self.assertEqual(update_response.status_code, status.HTTP_200_OK)

        # check the first page of data
        url = f"/api/projects/{self.team.id}/feature_flags/{flag_id}/activity"
        first_page_response = self.client.get(url)
        self.assertEqual(first_page_response.status_code, status.HTTP_200_OK)
        first_page_json = first_page_response.json()

        self.assertEqual(
            # feature flag activity writes the flag key to the detail name
            [log_item["detail"]["name"] for log_item in first_page_json["results"]],
            ["14", "13", "12", "11", "10", "9", "8", "7", "6", "5"],
        )
        self.assertEqual(
            first_page_json["next"],
            f"http://testserver/api/projects/{self.team.id}/feature_flags/{flag_id}/activity?page=2&limit=10",
        )
        self.assertEqual(first_page_json["previous"], None)

        # check the second page of data
        second_page_response = self.client.get(first_page_json["next"])
        self.assertEqual(second_page_response.status_code, status.HTTP_200_OK)
        second_page_json = second_page_response.json()

        self.assertEqual(
            # feature flag activity writes the flag key to the detail name
            [log_item["detail"]["name"] for log_item in second_page_json["results"]],
            ["4", "3", "2", "1", "0"],
        )
        self.assertEqual(second_page_json["next"], None)
        self.assertEqual(
            second_page_json["previous"],
            f"http://testserver/api/projects/{self.team.id}/feature_flags/{flag_id}/activity?page=1&limit=10",
        )

    def test_get_flags_with_specified_token(self):
        _, _, user = User.objects.bootstrap("Test", "team2@posthog.com", None)
        self.client.force_login(user)
        assert user.team is not None
        assert self.team is not None
        self.assertNotEqual(user.team.id, self.team.id)

        response_team_1 = self.client.get(f"/api/projects/@current/feature_flags")
        response_team_1_token = self.client.get(f"/api/projects/@current/feature_flags?token={user.team.api_token}")
        response_team_2 = self.client.get(f"/api/projects/@current/feature_flags?token={self.team.api_token}")

        self.assertEqual(response_team_1.json(), response_team_1_token.json())
        self.assertNotEqual(response_team_1.json(), response_team_2.json())

        response_invalid_token = self.client.get(f"/api/projects/@current/feature_flags?token=invalid")
        self.assertEqual(response_invalid_token.status_code, 401)

    def test_soft_delete_flag_renames_key_and_allows_reuse(self):
        # Create flag and experiment, then soft-delete experiment
        flag = FeatureFlag.objects.create(team=self.team, created_by=self.user, key="flag1")
        exp = Experiment.objects.create(team=self.team, created_by=self.user, feature_flag=flag)
        exp.deleted = True
        exp.save()
        # Soft-delete flag: should rename key
        response = self.client.patch(f"/api/projects/{self.team.id}/feature_flags/{flag.id}/", {"deleted": True})
        assert response.status_code == 200
        flag.refresh_from_db()
        assert flag.deleted is True
        assert flag.key == f"flag1:deleted:{flag.id}"
        # Should now be able to create a new flag with the original key
        response = self.client.post(f"/api/projects/{self.team.id}/feature_flags/", {"name": "Flag1", "key": "flag1"})
        assert response.status_code == 201
        assert response.json()["key"] == "flag1"

    def test_soft_delete_flag_blocked_with_active_experiment(self):
        flag = FeatureFlag.objects.create(team=self.team, created_by=self.user, key="flag2")
        exp = Experiment.objects.create(team=self.team, created_by=self.user, feature_flag=flag)
        response = self.client.patch(f"/api/projects/{self.team.id}/feature_flags/{flag.id}/", {"deleted": True})
        assert response.status_code == 400
        assert (
            response.json()["detail"]
            == f"Cannot delete a feature flag that is linked to active experiment(s) with ID(s): {exp.id}. Please delete the experiment(s) before deleting the flag."
        )

    def test_my_flags_is_not_nplus1(self) -> None:
        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "name": f"flag",
                "key": f"flag",
                "filters": {"groups": [{"rollout_percentage": 5}]},
            },
            format="json",
        ).json()

        with self.assertNumQueries(FuzzyInt(8, 9)):
            response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/my_flags")
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        for i in range(1, 4):
            self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                data={
                    "name": f"flag",
                    "key": f"flag_{i}",
                    "filters": {"groups": [{"rollout_percentage": 5}]},
                },
                format="json",
            ).json()

        with self.assertNumQueries(FuzzyInt(8, 9)):
            response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/my_flags")
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_getting_flags_is_not_nplus1(self) -> None:
        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "name": f"flag",
                "key": f"flag_0",
                "filters": {"groups": [{"rollout_percentage": 5}]},
            },
            format="json",
        ).json()

        with self.assertNumQueries(FuzzyInt(16, 17)):
            response = self.client.get(f"/api/projects/{self.team.id}/feature_flags")
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        for i in range(1, 5):
            self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/",
                data={
                    "name": f"flag",
                    "key": f"flag_{i}",
                    "filters": {"groups": [{"rollout_percentage": 5}]},
                },
                format="json",
            ).json()

        with self.assertNumQueries(FuzzyInt(16, 17)):
            response = self.client.get(f"/api/projects/{self.team.id}/feature_flags")
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_getting_flags_with_no_creator(self) -> None:
        FeatureFlag.objects.all().delete()

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "name": f"flag",
                "key": f"flag_0",
                "filters": {"groups": [{"rollout_percentage": 5}]},
            },
            format="json",
        ).json()

        FeatureFlag.objects.create(
            created_by=None,
            team=self.team,
            key="flag_role_access",
            name="Flag role access",
        )

        with self.assertNumQueries(FuzzyInt(16, 17)):
            response = self.client.get(f"/api/projects/{self.team.id}/feature_flags")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.json()["results"]), 2)
            sorted_results = sorted(response.json()["results"], key=lambda x: x["key"])
            self.assertEqual(sorted_results[1]["created_by"], None)
            self.assertEqual(sorted_results[1]["key"], "flag_role_access")

    @patch("posthog.api.feature_flag.report_user_action")
    def test_my_flags(self, mock_capture):
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [{"rollout_percentage": 20}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )

        # # alpha-feature is set for "distinct_id"
        distinct_id_user = User.objects.create_and_join(self.organization, "distinct_id_user@posthog.com", None)
        distinct_id_user.distinct_id = "distinct_id"
        distinct_id_user.save()
        self.client.force_login(distinct_id_user)
        response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/my_flags")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data), 2)

        first_flag = response_data[0]
        self.assertEqual(first_flag["feature_flag"]["key"], "alpha-feature")
        self.assertEqual(first_flag["value"], "third-variant")

        second_flag = response_data[1]
        self.assertEqual(second_flag["feature_flag"]["key"], "red_button")
        self.assertEqual(second_flag["value"], True)

        # alpha-feature is not set for "distinct_id_0"
        distinct_id_0_user = User.objects.create_and_join(self.organization, "distinct_id_0_user@posthog.com", None)
        distinct_id_0_user.distinct_id = "distinct_id_0"
        distinct_id_0_user.save()
        self.client.force_login(distinct_id_0_user)
        response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/my_flags")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data), 2)

        first_flag = response_data[0]
        self.assertEqual(first_flag["feature_flag"]["key"], "alpha-feature")
        self.assertEqual(first_flag["value"], False)

    @patch("posthog.api.feature_flag.report_user_action")
    def test_my_flags_empty_flags(self, mock_capture):
        # Ensure empty feature flag list
        FeatureFlag.objects.all().delete()

        response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/my_flags")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data), 0)

    @patch("posthoganalytics.capture")
    def test_my_flags_groups(self, mock_capture):
        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "groups flag",
                "key": "groups-flag",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [{"rollout_percentage": 100}],
                },
            },
            format="json",
        )

        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )

        response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/my_flags")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        groups_flag = response.json()[0]
        self.assertEqual(groups_flag["feature_flag"]["key"], "groups-flag")
        self.assertEqual(groups_flag["value"], False)

        response = self.client.get(
            f"/api/projects/{self.team.id}/feature_flags/my_flags",
            data={"groups": json.dumps({"organization": "7"})},
        )
        groups_flag = response.json()[0]
        self.assertEqual(groups_flag["feature_flag"]["key"], "groups-flag")
        self.assertEqual(groups_flag["value"], True)

    @freeze_time("2021-08-25T22:09:14.252Z")
    @patch("posthog.api.feature_flag.report_user_action")
    def test_create_feature_flag_usage_dashboard(self, mock_capture):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {"groups": [{"rollout_percentage": 50}]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        flag_id = response.json()["id"]
        instance = FeatureFlag.objects.get(id=flag_id)
        self.assertEqual(instance.key, "alpha-feature")

        dashboard = instance.usage_dashboard
        assert dashboard is not None
        assert dashboard.tiles is not None
        tiles = sorted(dashboard.tiles.all(), key=lambda x: str(x.insight.name if x.insight is not None else ""))

        self.assertEqual(dashboard.name, "Generated Dashboard: alpha-feature Usage")
        self.assertEqual(
            dashboard.description,
            "This dashboard was generated by the feature flag with key (alpha-feature)",
        )
        assert dashboard is not None, "Usage dashboard was not created"
        self.assertEqual(dashboard.creation_mode, Dashboard.CreationMode.TEMPLATE)
        self.assertEqual(dashboard.filters, {"date_from": "-30d"})
        self.assertEqual(len(tiles), 2)
        assert tiles[0].insight is not None
        self.assertEqual(tiles[0].insight.name, "Feature Flag Called Total Volume")
        self.assertEqual(
            tiles[0].insight.query,
            {
                "kind": "InsightVizNode",
                "source": {
                    "kind": "TrendsQuery",
                    "series": [{"kind": "EventsNode", "name": "$feature_flag_called", "event": "$feature_flag_called"}],
                    "interval": "day",
                    "dateRange": {"date_from": "-30d", "explicitDate": False},
                    "properties": {
                        "type": "AND",
                        "values": [
                            {
                                "type": "AND",
                                "values": [
                                    {
                                        "key": "$feature_flag",
                                        "type": "event",
                                        "value": "alpha-feature",
                                        "operator": "exact",
                                    }
                                ],
                            }
                        ],
                    },
                    "trendsFilter": {
                        "display": "ActionsLineGraph",
                        "showLegend": False,
                        "yAxisScaleType": "linear",
                        "showValuesOnSeries": False,
                        "smoothingIntervals": 1,
                        "showPercentStackView": False,
                        "aggregationAxisFormat": "numeric",
                        "showAlertThresholdLines": False,
                    },
                    "breakdownFilter": {"breakdown": "$feature_flag_response", "breakdown_type": "event"},
                    "filterTestAccounts": False,
                },
            },
        )
        assert tiles[1].insight is not None
        self.assertEqual(tiles[1].insight.name, "Feature Flag calls made by unique users per variant")
        self.assertEqual(
            tiles[1].insight.query,
            {
                "kind": "InsightVizNode",
                "source": {
                    "kind": "TrendsQuery",
                    "series": [
                        {
                            "kind": "EventsNode",
                            "math": "dau",
                            "name": "$feature_flag_called",
                            "event": "$feature_flag_called",
                        }
                    ],
                    "interval": "day",
                    "dateRange": {"date_from": "-30d", "explicitDate": False},
                    "properties": {
                        "type": "AND",
                        "values": [
                            {
                                "type": "AND",
                                "values": [
                                    {
                                        "key": "$feature_flag",
                                        "type": "event",
                                        "value": "alpha-feature",
                                        "operator": "exact",
                                    }
                                ],
                            }
                        ],
                    },
                    "trendsFilter": {
                        "display": "ActionsTable",
                        "showLegend": False,
                        "yAxisScaleType": "linear",
                        "showValuesOnSeries": False,
                        "smoothingIntervals": 1,
                        "showPercentStackView": False,
                        "aggregationAxisFormat": "numeric",
                        "showAlertThresholdLines": False,
                    },
                    "breakdownFilter": {"breakdown": "$feature_flag_response", "breakdown_type": "event"},
                    "filterTestAccounts": False,
                },
            },
        )

        # now enable enriched analytics
        instance.has_enriched_analytics = True
        instance.save()

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/{flag_id}/enrich_usage_dashboard",
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        instance.refresh_from_db()

        dashboard = instance.usage_dashboard
        assert dashboard is not None
        assert dashboard.tiles is not None
        tiles = sorted(dashboard.tiles.all(), key=lambda x: str(x.insight.name if x.insight is not None else ""))

        self.assertEqual(dashboard.name, "Generated Dashboard: alpha-feature Usage")
        self.assertEqual(
            dashboard.description,
            "This dashboard was generated by the feature flag with key (alpha-feature)",
        )
        self.assertEqual(dashboard.filters, {"date_from": "-30d"})
        self.assertEqual(len(tiles), 4)
        assert tiles[0].insight is not None
        self.assertEqual(tiles[0].insight.name, "Feature Flag Called Total Volume")
        self.assertEqual(
            tiles[0].insight.query,
            {
                "kind": "InsightVizNode",
                "source": {
                    "kind": "TrendsQuery",
                    "series": [{"kind": "EventsNode", "name": "$feature_flag_called", "event": "$feature_flag_called"}],
                    "interval": "day",
                    "dateRange": {"date_from": "-30d", "explicitDate": False},
                    "properties": {
                        "type": "AND",
                        "values": [
                            {
                                "type": "AND",
                                "values": [
                                    {
                                        "key": "$feature_flag",
                                        "type": "event",
                                        "value": "alpha-feature",
                                        "operator": "exact",
                                    }
                                ],
                            }
                        ],
                    },
                    "trendsFilter": {
                        "display": "ActionsLineGraph",
                        "showLegend": False,
                        "yAxisScaleType": "linear",
                        "showValuesOnSeries": False,
                        "smoothingIntervals": 1,
                        "showPercentStackView": False,
                        "aggregationAxisFormat": "numeric",
                        "showAlertThresholdLines": False,
                    },
                    "breakdownFilter": {"breakdown": "$feature_flag_response", "breakdown_type": "event"},
                    "filterTestAccounts": False,
                },
            },
        )
        assert tiles[1].insight is not None
        self.assertEqual(tiles[1].insight.name, "Feature Flag calls made by unique users per variant")
        self.assertEqual(
            tiles[1].insight.query,
            {
                "kind": "InsightVizNode",
                "source": {
                    "kind": "TrendsQuery",
                    "series": [
                        {
                            "kind": "EventsNode",
                            "math": "dau",
                            "name": "$feature_flag_called",
                            "event": "$feature_flag_called",
                        }
                    ],
                    "interval": "day",
                    "dateRange": {"date_from": "-30d", "explicitDate": False},
                    "properties": {
                        "type": "AND",
                        "values": [
                            {
                                "type": "AND",
                                "values": [
                                    {
                                        "key": "$feature_flag",
                                        "type": "event",
                                        "value": "alpha-feature",
                                        "operator": "exact",
                                    }
                                ],
                            }
                        ],
                    },
                    "trendsFilter": {
                        "display": "ActionsTable",
                        "showLegend": False,
                        "yAxisScaleType": "linear",
                        "showValuesOnSeries": False,
                        "smoothingIntervals": 1,
                        "showPercentStackView": False,
                        "aggregationAxisFormat": "numeric",
                        "showAlertThresholdLines": False,
                    },
                    "breakdownFilter": {"breakdown": "$feature_flag_response", "breakdown_type": "event"},
                    "filterTestAccounts": False,
                },
            },
        )

        # enriched insights
        assert tiles[2].insight is not None
        self.assertEqual(tiles[2].insight.name, "Feature Interaction Total Volume")
        self.assertEqual(
            tiles[2].insight.query,
            {
                "kind": "InsightVizNode",
                "source": {
                    "kind": "TrendsQuery",
                    "series": [
                        {"kind": "EventsNode", "name": "Feature Interaction - Total", "event": "$feature_interaction"},
                        {
                            "kind": "EventsNode",
                            "math": "dau",
                            "name": "Feature Interaction - Unique users",
                            "event": "$feature_interaction",
                        },
                    ],
                    "interval": "day",
                    "dateRange": {"date_from": "-30d", "explicitDate": False},
                    "properties": {
                        "type": "AND",
                        "values": [
                            {
                                "type": "AND",
                                "values": [
                                    {
                                        "key": "feature_flag",
                                        "type": "event",
                                        "value": "alpha-feature",
                                        "operator": "exact",
                                    }
                                ],
                            }
                        ],
                    },
                    "trendsFilter": {
                        "display": "ActionsLineGraph",
                        "showLegend": False,
                        "yAxisScaleType": "linear",
                        "showValuesOnSeries": False,
                        "smoothingIntervals": 1,
                        "showPercentStackView": False,
                        "aggregationAxisFormat": "numeric",
                        "showAlertThresholdLines": False,
                    },
                    "breakdownFilter": {"breakdown_type": "event"},
                    "filterTestAccounts": False,
                },
            },
        )
        assert tiles[3].insight is not None
        self.assertEqual(tiles[3].insight.name, "Feature Viewed Total Volume")
        self.assertEqual(
            tiles[3].insight.query,
            {
                "kind": "InsightVizNode",
                "source": {
                    "kind": "TrendsQuery",
                    "series": [
                        {"kind": "EventsNode", "name": "Feature View - Total", "event": "$feature_view"},
                        {
                            "kind": "EventsNode",
                            "math": "dau",
                            "name": "Feature View - Unique users",
                            "event": "$feature_view",
                        },
                    ],
                    "interval": "day",
                    "dateRange": {"date_from": "-30d", "explicitDate": False},
                    "properties": {
                        "type": "AND",
                        "values": [
                            {
                                "type": "AND",
                                "values": [
                                    {
                                        "key": "feature_flag",
                                        "type": "event",
                                        "value": "alpha-feature",
                                        "operator": "exact",
                                    }
                                ],
                            }
                        ],
                    },
                    "trendsFilter": {
                        "display": "ActionsLineGraph",
                        "showLegend": False,
                        "yAxisScaleType": "linear",
                        "showValuesOnSeries": False,
                        "smoothingIntervals": 1,
                        "showPercentStackView": False,
                        "aggregationAxisFormat": "numeric",
                        "showAlertThresholdLines": False,
                    },
                    "breakdownFilter": {"breakdown_type": "event"},
                    "filterTestAccounts": False,
                },
            },
        )

    @freeze_time("2021-08-25T22:09:14.252Z")
    @patch("posthog.api.feature_flag.report_user_action")
    def test_dashboard_enrichment_fails_if_already_enriched(self, mock_capture):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {"groups": [{"rollout_percentage": 50}]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        flag_id = response.json()["id"]
        instance = FeatureFlag.objects.get(id=flag_id)
        self.assertEqual(instance.key, "alpha-feature")

        # now enable enriched analytics
        instance.has_enriched_analytics = True
        instance.save()

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/{flag_id}/enrich_usage_dashboard",
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # now try enriching again
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/{flag_id}/enrich_usage_dashboard",
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {"error": "Usage dashboard already has enriched data", "success": False},
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_dashboard_enrichment_fails_if_no_enriched_data(self, mock_capture):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {"groups": [{"rollout_percentage": 50}]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        flag_id = response.json()["id"]
        instance = FeatureFlag.objects.get(id=flag_id)
        self.assertEqual(instance.key, "alpha-feature")

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/{flag_id}/enrich_usage_dashboard",
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "error": "No enriched analytics available for this feature flag",
                "success": False,
            },
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_local_evaluation(self, mock_capture):
        FeatureFlag.objects.all().delete()
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="company", group_type_index=1
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [{"rollout_percentage": 20}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Group feature",
                "key": "group-feature",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [{"rollout_percentage": 21}],
                },
            },
            format="json",
        )

        # old style feature flags
        FeatureFlag.objects.create(
            name="Beta feature",
            key="beta-feature",
            team=self.team,
            rollout_percentage=51,
            filters={"properties": [{"key": "beta-property", "value": "beta-value"}]},
            created_by=self.user,
        )
        # and inactive flag
        FeatureFlag.objects.create(
            name="Inactive feature",
            key="inactive-flag",
            team=self.team,
            active=False,
            rollout_percentage=100,
            filters={"properties": []},
            created_by=self.user,
        )

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        self.client.logout()
        # `local_evaluation` is called by logged out clients!

        # missing API key
        response = self.client.get(f"/api/feature_flag/local_evaluation?token={self.team.api_token}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        response = self.client.get(f"/api/feature_flag/local_evaluation")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        response = self.client.get(
            f"/api/feature_flag/local_evaluation",
            HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertTrue("flags" in response_data and "group_type_mapping" in response_data)
        self.assertEqual(len(response_data["flags"]), 4)

        sorted_flags = sorted(response_data["flags"], key=lambda x: x["key"])

        self.assertDictContainsSubset(
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [{"rollout_percentage": 20}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
                "deleted": False,
                "active": True,
                "ensure_experience_continuity": False,
            },
            sorted_flags[0],
        )
        self.assertDictContainsSubset(
            {
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {
                    "groups": [
                        {
                            "properties": [{"key": "beta-property", "value": "beta-value"}],
                            "rollout_percentage": 51,
                        }
                    ]
                },
                "deleted": False,
                "active": True,
                "ensure_experience_continuity": False,
            },
            sorted_flags[1],
        )
        self.assertDictContainsSubset(
            {
                "name": "Group feature",
                "key": "group-feature",
                "filters": {
                    "groups": [{"rollout_percentage": 21}],
                    "aggregation_group_type_index": 0,
                },
                "deleted": False,
                "active": True,
                "ensure_experience_continuity": False,
            },
            sorted_flags[2],
        )

        self.assertEqual(response_data["group_type_mapping"], {"0": "organization", "1": "company"})

    @patch("posthog.api.feature_flag.report_user_action")
    def test_local_evaluation_with_secret_api_key(self, mock_capture):
        FeatureFlag.objects.all().delete()

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [{"rollout_percentage": 20}],
                    "multivariate": {},
                },
            },
            format="json",
        )

        secret_api_key = generate_random_token_secret()
        self.team.secret_api_token = secret_api_key
        self.team.save()

        self.client.logout()  # `local_evaluation` is called by logged out clients!

        response = self.client.get(
            f"/api/feature_flag/local_evaluation",
            data={"secret_api_key": secret_api_key},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(f"/api/feature_flag/local_evaluation?secret_api_key={secret_api_key}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(
            f"/api/feature_flag/local_evaluation",
            HTTP_AUTHORIZATION=f"Bearer {secret_api_key}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertTrue("flags" in response_data)
        self.assertEqual(len(response_data["flags"]), 1)

        response = self.client.get(
            f"/api/feature_flag/local_evaluation",
            HTTP_AUTHORIZATION=f"Bearer phs_0000000000000000000000000000",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("posthog.api.feature_flag.report_user_action")
    def test_local_evaluation_for_cohorts(self, mock_capture):
        FeatureFlag.objects.all().delete()

        cohort_valid_for_ff = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {
                                    "key": "$some_prop",
                                    "value": "nomatchihope",
                                    "type": "person",
                                },
                                {
                                    "key": "$some_prop2",
                                    "value": "nomatchihope2",
                                    "type": "person",
                                },
                            ],
                        }
                    ],
                }
            },
            name="cohort1",
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 20,
                            "properties": [
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_valid_for_ff.pk,
                                }
                            ],
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        self.client.logout()

        response = self.client.get(
            f"/api/feature_flag/local_evaluation?token={self.team.api_token}",
            HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertTrue("flags" in response_data and "group_type_mapping" in response_data)
        self.assertEqual(len(response_data["flags"]), 1)

        sorted_flags = sorted(response_data["flags"], key=lambda x: x["key"])

        self.assertDictContainsSubset(
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "$some_prop",
                                    "type": "person",
                                    "value": "nomatchihope",
                                }
                            ],
                            "rollout_percentage": 20,
                        },
                        {
                            "properties": [
                                {
                                    "key": "$some_prop2",
                                    "type": "person",
                                    "value": "nomatchihope2",
                                }
                            ],
                            "rollout_percentage": 20,
                        },
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
                "deleted": False,
                "active": True,
                "ensure_experience_continuity": False,
            },
            sorted_flags[0],
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_local_evaluation_for_invalid_cohorts(self, mock_capture):
        FeatureFlag.objects.all().delete()

        self.team.app_urls = ["https://example.com"]
        self.team.save()

        other_team = Team.objects.create(
            organization=self.organization,
            api_token="bazinga_new",
            name="New Team",
        )

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        deleted_cohort = Cohort.objects.create(
            team=self.team,
            groups=[
                {
                    "properties": [
                        {
                            "key": "$some_prop_1",
                            "value": "something_1",
                            "type": "person",
                        }
                    ]
                },
            ],
            name="cohort1",
            deleted=True,
        )

        cohort_from_other_team = Cohort.objects.create(
            team=other_team,
            groups=[
                {
                    "properties": [
                        {
                            "key": "$some_prop_1",
                            "value": "something_1",
                            "type": "person",
                        }
                    ]
                },
            ],
            name="cohort1",
        )

        cohort_with_nested_invalid = Cohort.objects.create(
            team=self.team,
            groups=[
                {
                    "properties": [
                        {
                            "key": "$some_prop_1",
                            "value": "something_1",
                            "type": "person",
                        },
                        {
                            "key": "id",
                            "value": 99999,
                            "type": "cohort",
                        },
                        {
                            "key": "id",
                            "value": deleted_cohort.pk,
                            "type": "cohort",
                        },
                        {
                            "key": "id",
                            "value": cohort_from_other_team.pk,
                            "type": "cohort",
                        },
                    ]
                },
            ],
            name="cohort1",
        )

        cohort_valid = Cohort.objects.create(
            team=self.team,
            groups=[
                {
                    "properties": [
                        {
                            "key": "$some_prop_1",
                            "value": "something_1",
                            "type": "person",
                        },
                    ]
                },
            ],
            name="cohort1",
        )

        FeatureFlag.objects.create(
            team=self.team,
            filters={"groups": [{"properties": [{"key": "id", "value": 99999, "type": "cohort"}]}]},
            name="This is a cohort-based flag",
            key="cohort-flag",
            created_by=self.user,
        )
        FeatureFlag.objects.create(
            team=self.team,
            filters={
                "groups": [{"properties": [{"key": "id", "value": cohort_with_nested_invalid.pk, "type": "cohort"}]}]
            },
            name="This is a cohort-based flag",
            key="cohort-flag-2",
            created_by=self.user,
        )
        FeatureFlag.objects.create(
            team=self.team,
            filters={"groups": [{"properties": [{"key": "id", "value": cohort_from_other_team.pk, "type": "cohort"}]}]},
            name="This is a cohort-based flag",
            key="cohort-flag-3",
            created_by=self.user,
        )
        FeatureFlag.objects.create(
            team=self.team,
            filters={
                "groups": [
                    {"properties": [{"key": "id", "value": cohort_valid.pk, "type": "cohort"}]},
                    {"properties": [{"key": "id", "value": cohort_with_nested_invalid.pk, "type": "cohort"}]},
                    {"properties": [{"key": "id", "value": 99999, "type": "cohort"}]},
                    {"properties": [{"key": "id", "value": deleted_cohort.pk, "type": "cohort"}]},
                ]
            },
            name="This is a cohort-based flag",
            key="cohort-flag-4",
            created_by=self.user,
        )
        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 100,
                            "properties": [],
                        }
                    ],
                },
            },
            format="json",
        )

        self.client.logout()

        with self.assertNumQueries(18):
            # 1. SAVEPOINT
            # 2. SELECT "posthog_personalapikey"."id",
            # 3. RELEASE SAVEPOINT
            # 4. UPDATE "posthog_personalapikey" SET "last_used_at"
            # 5. SELECT "posthog_team"."id", "posthog_team"."uuid",
            # 6. SELECT "posthog_team"."id", "posthog_team"."uuid",
            # 7. SELECT "posthog_project"."id", "posthog_project"."organization_id",
            # 8. SELECT "posthog_organizationmembership"."id",
            # 9. SELECT "ee_accesscontrol"."id",
            # 10. SELECT "posthog_organizationmembership"."id",
            # 11. SELECT "posthog_cohort"."id"  -- all cohorts
            # 12. SELECT "posthog_featureflag"."id", "posthog_featureflag"."key", -- all flags
            # 13. SELECT "posthog_team"."id", "posthog_team"."uuid",
            # 14. SELECT "posthog_cohort". id = 99999
            # 15. SELECT "posthog_team"."id", "posthog_team"."uuid",
            # 16. SELECT "posthog_cohort". id = deleted cohort
            # 17. SELECT "posthog_cohort". id = cohort from other team
            # 18. SELECT "posthog_grouptypemapping"."id", -- group type mapping

            response = self.client.get(
                f"/api/feature_flag/local_evaluation?token={self.team.api_token}&send_cohorts",
                HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertTrue("flags" in response_data and "group_type_mapping" in response_data)
        self.assertEqual(len(response_data["flags"]), 5)
        self.assertEqual(len(response_data["cohorts"]), 2)
        assert str(cohort_valid.pk) in response_data["cohorts"]
        assert str(cohort_with_nested_invalid.pk) in response_data["cohorts"]

    @patch("posthog.api.feature_flag.report_user_action")
    def test_local_evaluation_for_cohorts_with_variant_overrides(self, mock_capture):
        FeatureFlag.objects.all().delete()

        cohort_valid_for_ff = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "AND",
                            "values": [
                                {
                                    "key": "$some_prop",
                                    "value": "nomatchihope",
                                    "type": "person",
                                },
                            ],
                        }
                    ],
                }
            },
            name="cohort1",
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "variant": "test",
                            "properties": [
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_valid_for_ff.pk,
                                }
                            ],
                            "rollout_percentage": 100,
                        },
                        {
                            "variant": "test",
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "@posthog.com",
                                    "operator": "icontains",
                                }
                            ],
                            "rollout_percentage": 100,
                        },
                    ],
                    "multivariate": {
                        "variants": [
                            {"key": "control", "name": "", "rollout_percentage": 100},
                            {"key": "test", "name": "", "rollout_percentage": 0},
                        ]
                    },
                },
            },
            format="json",
        )

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        self.client.logout()

        response = self.client.get(
            f"/api/feature_flag/local_evaluation?token={self.team.api_token}",
            HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertTrue("flags" in response_data and "group_type_mapping" in response_data)
        self.assertEqual(len(response_data["flags"]), 1)

        sorted_flags = sorted(response_data["flags"], key=lambda x: x["key"])

        self.assertDictContainsSubset(
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "variant": "test",
                            "properties": [
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_valid_for_ff.pk,
                                }
                            ],
                            "rollout_percentage": 100,
                        },
                        {
                            "variant": "test",
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "@posthog.com",
                                    "operator": "icontains",
                                }
                            ],
                            "rollout_percentage": 100,
                        },
                    ],
                    "multivariate": {
                        "variants": [
                            {"key": "control", "name": "", "rollout_percentage": 100},
                            {"key": "test", "name": "", "rollout_percentage": 0},
                        ]
                    },
                },
                "deleted": False,
                "active": True,
                "ensure_experience_continuity": False,
            },
            sorted_flags[0],
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_local_evaluation_for_static_cohorts(self, mock_capture):
        FeatureFlag.objects.all().delete()

        cohort_valid_for_ff = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="cohort1",
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 20,
                            "properties": [
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_valid_for_ff.pk,
                                }
                            ],
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        response = self.client.get(
            f"/api/feature_flag/local_evaluation?token={self.team.api_token}&send_cohorts",
            HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertTrue("flags" in response_data and "group_type_mapping" in response_data)
        self.assertEqual(len(response_data["flags"]), 1)

        sorted_flags = sorted(response_data["flags"], key=lambda x: x["key"])

        self.assertDictContainsSubset(
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 20,
                            "properties": [
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_valid_for_ff.pk,
                                }
                            ],
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
                "deleted": False,
                "active": True,
                "ensure_experience_continuity": False,
            },
            sorted_flags[0],
        )

        self.assertEqual(
            response_data["cohorts"],
            {},
        )

    @patch("posthog.api.feature_flag.report_user_action")
    def test_local_evaluation_for_arbitrary_cohorts(self, mock_capture):
        FeatureFlag.objects.all().delete()

        cohort_valid_for_ff = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {
                                    "key": "$some_prop",
                                    "value": "nomatchihope",
                                    "type": "person",
                                },
                                {
                                    "key": "$some_prop2",
                                    "value": "nomatchihope2",
                                    "type": "person",
                                },
                            ],
                        }
                    ],
                }
            },
            name="cohort1",
        )

        cohort2 = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {
                                    "key": "$some_prop",
                                    "value": "nomatchihope",
                                    "type": "person",
                                },
                                {
                                    "key": "$some_prop2",
                                    "value": "nomatchihope2",
                                    "type": "person",
                                },
                                {
                                    "key": "id",
                                    "value": cohort_valid_for_ff.pk,
                                    "type": "cohort",
                                    "negation": True,
                                },
                            ],
                        }
                    ],
                }
            },
            name="cohort2",
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 20,
                            "properties": [{"key": "id", "type": "cohort", "value": cohort2.pk}],
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            },
            format="json",
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature-2",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 20,
                            "properties": [
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_valid_for_ff.pk,
                                }
                            ],
                        }
                    ],
                },
            },
            format="json",
        )

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        response = self.client.get(
            f"/api/feature_flag/local_evaluation?token={self.team.api_token}&send_cohorts",
            HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertTrue(
            "flags" in response_data and "group_type_mapping" in response_data and "cohorts" in response_data
        )
        self.assertEqual(len(response_data["flags"]), 2)

        sorted_flags = sorted(response_data["flags"], key=lambda x: x["key"])

        self.assertEqual(
            response_data["cohorts"],
            {
                str(cohort_valid_for_ff.pk): {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {
                                    "key": "$some_prop",
                                    "type": "person",
                                    "value": "nomatchihope",
                                },
                                {
                                    "key": "$some_prop2",
                                    "type": "person",
                                    "value": "nomatchihope2",
                                },
                            ],
                        }
                    ],
                },
                str(cohort2.pk): {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {
                                    "key": "$some_prop",
                                    "type": "person",
                                    "value": "nomatchihope",
                                },
                                {
                                    "key": "$some_prop2",
                                    "type": "person",
                                    "value": "nomatchihope2",
                                },
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_valid_for_ff.pk,
                                    "negation": True,
                                },
                            ],
                        }
                    ],
                },
            },
        )

        self.assertDictContainsSubset(
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 20,
                            "properties": [{"key": "id", "type": "cohort", "value": cohort2.pk}],
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
                "deleted": False,
                "active": True,
                "ensure_experience_continuity": False,
            },
            sorted_flags[0],
        )

        self.assertDictContainsSubset(
            {
                "name": "Alpha feature",
                "key": "alpha-feature-2",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_valid_for_ff.pk,
                                }
                            ],
                            "rollout_percentage": 20,
                        },
                    ],
                },
                "deleted": False,
                "active": True,
                "ensure_experience_continuity": False,
            },
            sorted_flags[1],
        )

    @patch("posthog.models.feature_flag.flag_analytics.CACHE_BUCKET_SIZE", 10)
    def test_local_evaluation_billing_analytics(self):
        FeatureFlag.objects.all().delete()

        # old style feature flags
        FeatureFlag.objects.create(
            name="Beta feature",
            key="beta-feature",
            team=self.team,
            rollout_percentage=51,
            filters={"properties": [{"key": "beta-property", "value": "beta-value"}]},
            created_by=self.user,
        )
        # and inactive flag
        FeatureFlag.objects.create(
            name="Inactive feature",
            key="inactive-flag",
            team=self.team,
            active=False,
            rollout_percentage=100,
            filters={"properties": []},
            created_by=self.user,
        )

        client = redis.get_client()

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        self.client.logout()
        # `local_evaluation` is called by logged out clients!

        with freeze_time("2022-05-07 12:23:07"):
            # missing API key
            response = self.client.get(f"/api/feature_flag/local_evaluation?token={self.team.api_token}")
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"), {})

            response = self.client.get(f"/api/feature_flag/local_evaluation")
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"), {})

            response = self.client.get(
                f"/api/feature_flag/local_evaluation?token={self.team.api_token}",
                HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(
                client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"),
                {b"165192618": b"1"},
            )

            for _ in range(5):
                response = self.client.get(
                    f"/api/feature_flag/local_evaluation?token={self.team.api_token}",
                    HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)

            self.assertEqual(
                client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"),
                {b"165192618": b"6"},
            )

    @patch("posthog.models.feature_flag.flag_analytics.CACHE_BUCKET_SIZE", 10)
    def test_local_evaluation_billing_analytics_for_regular_feature_flag_list(self):
        FeatureFlag.objects.all().delete()

        # old style feature flags
        FeatureFlag.objects.create(
            name="Beta feature",
            key="beta-feature",
            team=self.team,
            rollout_percentage=51,
            filters={"properties": [{"key": "beta-property", "value": "beta-value"}]},
            created_by=self.user,
        )
        # and inactive flag
        FeatureFlag.objects.create(
            name="Inactive feature",
            key="inactive-flag",
            team=self.team,
            active=False,
            rollout_percentage=100,
            filters={"properties": []},
            created_by=self.user,
        )

        client = redis.get_client()

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        # request made while logged in, via client cookie auth
        response = self.client.get(f"/api/feature_flag?token={self.team.api_token}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["count"], 2)

        # shouldn't add to local eval requests
        self.assertEqual(client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"), {})

        self.client.logout()
        # `local_evaluation` is called by logged out clients!

        with freeze_time("2022-05-07 12:23:07"):
            # missing API key
            response = self.client.get(f"/api/feature_flag?token={self.team.api_token}")
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"), {})

            response = self.client.get(f"/api/feature_flag/")
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"), {})

            response = self.client.get(
                f"/api/feature_flag/?token={self.team.api_token}",
                HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(
                client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"),
                {b"165192618": b"1"},
            )

            for _ in range(4):
                response = self.client.get(
                    f"/api/feature_flag/?token={self.team.api_token}",
                    HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)

            # local evaluation still works
            response = self.client.get(
                f"/api/feature_flag/local_evaluation?token={self.team.api_token}",
                HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            self.assertEqual(
                client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"),
                {b"165192618": b"6"},
            )

    @patch("posthog.api.feature_flag.settings.DECIDE_FEATURE_FLAG_QUOTA_CHECK", True)
    def test_local_evaluation_quota_limited(self):
        """Test that local evaluation returns 402 Payment Required when over quota."""
        from ee.billing.quota_limiting import QuotaLimitingCaches, QuotaResource

        # Set up a feature flag
        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {"groups": [{"rollout_percentage": 65}]},
            },
            format="json",
        )

        # Mock the quota limiting cache to simulate being over quota
        with patch(
            "ee.billing.quota_limiting.list_limited_team_attributes",
            return_value=[self.team.api_token],
        ) as mock_quota_limited:
            response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/local_evaluation")
            mock_quota_limited.assert_called_once_with(
                QuotaResource.FEATURE_FLAG_REQUESTS,
                QuotaLimitingCaches.QUOTA_LIMITER_CACHE_KEY,
            )

            self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
            response_data = response.json()

            # Verify the error response structure
            self.assertEqual(
                response_data,
                {
                    "type": "quota_limited",
                    "detail": "You have exceeded your feature flag request quota",
                    "code": "payment_required",
                },
            )

    @patch("posthog.api.feature_flag.settings.DECIDE_FEATURE_FLAG_QUOTA_CHECK", True)
    def test_local_evaluation_not_quota_limited(self):
        """Test that local evaluation returns normal response when not over quota."""
        from ee.billing.quota_limiting import QuotaLimitingCaches, QuotaResource

        # Set up a feature flag
        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {"groups": [{"rollout_percentage": 65}]},
            },
            format="json",
        )

        # Mock the quota limiting cache to simulate not being over quota
        with patch(
            "ee.billing.quota_limiting.list_limited_team_attributes",
            return_value=[],
        ) as mock_quota_limited:
            response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/local_evaluation")
            mock_quota_limited.assert_called_once_with(
                QuotaResource.FEATURE_FLAG_REQUESTS,
                QuotaLimitingCaches.QUOTA_LIMITER_CACHE_KEY,
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            response_data = response.json()

            # Verify normal response structure when not quota limited
            self.assertTrue(len(response_data["flags"]) > 0)
            self.assertNotIn("quotaLimited", response_data)

    @patch("posthog.api.feature_flag.settings.DECIDE_FEATURE_FLAG_QUOTA_CHECK", False)
    def test_local_evaluation_quota_check_disabled(self):
        """Test that local evaluation bypasses quota check when the setting is disabled."""

        # Set up a feature flag
        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {"groups": [{"rollout_percentage": 65}]},
            },
            format="json",
        )

        with patch(
            "ee.billing.quota_limiting.list_limited_team_attributes",
        ) as mock_quota_limited:
            response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/local_evaluation")

            # Verify quota limiting was not checked
            mock_quota_limited.assert_not_called()

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            response_data = response.json()

            # Verify normal response structure
            self.assertTrue(len(response_data["flags"]) > 0)
            self.assertNotIn("quotaLimited", response_data)

    @patch("posthog.api.feature_flag.settings.DECIDE_FEATURE_FLAG_QUOTA_CHECK", False)
    def test_local_evaluation_only_survey_targeting_flags(self):
        FeatureFlag.objects.all().delete()

        client = redis.get_client()

        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        with freeze_time("2022-05-07 12:23:07"):

            def make_request_and_assert_requests_recorded(expected_requests=None):
                response = self.client.get(
                    f"/api/feature_flag/local_evaluation?token={self.team.api_token}",
                    HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    client.hgetall(f"posthog:local_evaluation_requests:{self.team.pk}"),
                    expected_requests or {},
                )

            # No requests should be recorded if there are no flags
            make_request_and_assert_requests_recorded()

            FeatureFlag.objects.create(
                name="Survey Targeting Flag",
                key="survey-targeting-flag",
                team=self.team,
                filters={"properties": [{"key": "survey-targeting-property", "value": "survey-targeting-value"}]},
            )

            # No requests should be recorded if the only flags are survey targeting flags
            make_request_and_assert_requests_recorded()

            # Requests should be recorded if there is one regular feature flag
            FeatureFlag.objects.create(
                name="Regular Feature Flag",
                key="regular-feature-flag",
                team=self.team,
                filters={"properties": [{"key": "regular-property", "value": "regular-value"}]},
            )
            make_request_and_assert_requests_recorded({b"13766051": b"1"})

    @patch("posthog.api.feature_flag.report_user_action")
    def test_evaluation_reasons(self, mock_capture):
        FeatureFlag.objects.all().delete()
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="company", group_type_index=1
        )
        Person.objects.create(
            team_id=self.team.pk,
            distinct_ids=["1", "2"],
            properties={"beta-property": "beta-value"},
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [{"rollout_percentage": 20}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ],
                    },
                },
            },
            format="json",
        )

        # old style feature flags
        FeatureFlag.objects.create(
            name="Beta feature",
            key="beta-feature",
            team=self.team,
            rollout_percentage=81,
            filters={"properties": [{"key": "beta-property", "value": "beta-value"}]},
            created_by=self.user,
        )
        # and inactive flag
        FeatureFlag.objects.create(
            name="Inactive feature",
            key="inactive-flag",
            team=self.team,
            active=False,
            rollout_percentage=100,
            filters={"properties": []},
            created_by=self.user,
        )

        self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Group feature",
                "key": "group-feature",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [{"rollout_percentage": 61}],
                },
            },
            format="json",
        )

        # general test
        response = self.client.get(
            f"/api/projects/{self.team.pk}/feature_flags/evaluation_reasons",
            {
                "distinct_id": "test",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data), 4)

        self.assertEqual(
            response_data,
            {
                "alpha-feature": {
                    "value": False,
                    "evaluation": {
                        "reason": "out_of_rollout_bound",
                        "condition_index": 0,
                    },
                },
                "beta-feature": {
                    "value": False,
                    "evaluation": {
                        "reason": "no_condition_match",
                        "condition_index": 0,
                    },
                },
                "group-feature": {
                    "value": False,
                    "evaluation": {
                        "reason": "no_group_type",
                        "condition_index": None,
                    },
                },
                "inactive-flag": {
                    "value": False,
                    "evaluation": {
                        "reason": "disabled",
                        "condition_index": None,
                    },
                },
            },
        )

        # with person having beta-property for beta-feature
        # also matches alpha-feature as within rollout bounds
        response = self.client.get(
            f"/api/projects/{self.team.pk}/feature_flags/evaluation_reasons",
            {
                "distinct_id": "2",
                # "groups": json.dumps({"organization": "org1", "company": "company1"}),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data), 4)

        self.assertEqual(
            response_data,
            {
                "alpha-feature": {
                    "value": "first-variant",
                    "evaluation": {
                        "reason": "condition_match",
                        "condition_index": 0,
                    },
                },
                "beta-feature": {
                    "value": True,
                    "evaluation": {
                        "reason": "condition_match",
                        "condition_index": 0,
                    },
                },
                "group-feature": {
                    "value": False,
                    "evaluation": {
                        "reason": "no_group_type",
                        "condition_index": None,
                    },
                },
                "inactive-flag": {
                    "value": False,
                    "evaluation": {
                        "reason": "disabled",
                        "condition_index": None,
                    },
                },
            },
        )

        # with groups
        response = self.client.get(
            f"/api/projects/{self.team.pk}/feature_flags/evaluation_reasons",
            {
                "distinct_id": "org1234",
                "groups": json.dumps({"organization": "org1234"}),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertEqual(len(response_data), 4)

        self.assertEqual(
            response_data,
            {
                "alpha-feature": {
                    "value": False,
                    "evaluation": {
                        "reason": "out_of_rollout_bound",
                        "condition_index": 0,
                    },
                },
                "beta-feature": {
                    "value": False,
                    "evaluation": {
                        "reason": "no_condition_match",
                        "condition_index": 0,
                    },
                },
                "group-feature": {
                    "value": True,
                    "evaluation": {
                        "reason": "condition_match",
                        "condition_index": 0,
                    },
                },
                "inactive-flag": {
                    "value": False,
                    "evaluation": {
                        "reason": "disabled",
                        "condition_index": None,
                    },
                },
            },
        )

    def test_validation_person_properties(self):
        person_request = self._create_flag_with_properties(
            "person-flag",
            [
                {
                    "key": "email",
                    "type": "person",
                    "value": "@posthog.com",
                    "operator": "icontains",
                }
            ],
        )
        self.assertEqual(person_request.status_code, status.HTTP_201_CREATED)

        cohort: Cohort = Cohort.objects.create(team=self.team, name="My Cohort")
        cohort_request = self._create_flag_with_properties(
            "cohort-flag", [{"key": "id", "type": "cohort", "value": cohort.id}]
        )
        self.assertEqual(cohort_request.status_code, status.HTTP_201_CREATED)

        event_request = self._create_flag_with_properties(
            "illegal-event-flag",
            [{"key": "id", "value": 5}],
            expected_status=status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            event_request.json(),
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "Filters are not valid (can only use person, cohort, and flag properties)",
                "attr": "filters",
            },
        )

        groups_request = self._create_flag_with_properties(
            "illegal-groups-flag",
            [
                {
                    "key": "industry",
                    "value": "finance",
                    "type": "group",
                    "group_type_index": 0,
                }
            ],
            expected_status=status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            groups_request.json(),
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "Filters are not valid (can only use person, cohort, and flag properties)",
                "attr": "filters",
            },
        )

    def test_create_flag_with_invalid_date(self):
        resp = self._create_flag_with_properties(
            "date-flag",
            [
                {
                    "key": "created_for",
                    "type": "person",
                    "value": "6hed",
                    "operator": "is_date_before",
                }
            ],
            expected_status=status.HTTP_400_BAD_REQUEST,
        )

        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "invalid_date",
                "detail": "Invalid date value: 6hed",
                "attr": "filters",
            },
            resp.json(),
        )

        resp = self._create_flag_with_properties(
            "date-flag",
            [
                {
                    "key": "created_for",
                    "type": "person",
                    "value": "1234-02-993284",
                    "operator": "is_date_after",
                }
            ],
            expected_status=status.HTTP_400_BAD_REQUEST,
        )

        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "invalid_date",
                "detail": "Invalid date value: 1234-02-993284",
                "attr": "filters",
            },
            resp.json(),
        )

    def test_creating_feature_flag_with_non_existant_cohort(self):
        cohort_request = self._create_flag_with_properties(
            "cohort-flag",
            [{"key": "id", "type": "cohort", "value": 5151}],
            expected_status=status.HTTP_400_BAD_REQUEST,
        )

        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "cohort_does_not_exist",
                "detail": "Cohort with id 5151 does not exist",
                "attr": "filters",
            },
            cohort_request.json(),
        )

    def test_validation_payloads(self):
        self._create_flag_with_properties(
            "person-flag",
            [
                {
                    "key": "email",
                    "type": "person",
                    "value": "@posthog.com",
                    "operator": "icontains",
                }
            ],
            payloads={"true": 300},
            expected_status=status.HTTP_201_CREATED,
        )
        self._create_flag_with_properties(
            "person-flag",
            [
                {
                    "key": "email",
                    "type": "person",
                    "value": "@posthog.com",
                    "operator": "icontains",
                }
            ],
            payloads={"some-fake-key": 300},
            expected_status=status.HTTP_400_BAD_REQUEST,
        )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": None}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                    "payloads": {"first-variant": {"some": "payload"}},
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": None}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                    "payloads": {
                        "first-variant": {"some": "payload"},
                        "fourth-variant": {"some": "payload"},
                    },
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Multivariate feature",
                "key": "multivariate-feature",
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": None}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                    "payloads": {"first-variant": {"some": "payload"}, "true": 2500},
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        valid_json_payload = self._create_flag_with_properties(
            "json-flag",
            [{"key": "key", "value": "value", "type": "person"}],
            payloads={"true": json.dumps({"key": "value"})},
            expected_status=status.HTTP_201_CREATED,
        )
        self.assertEqual(valid_json_payload.status_code, status.HTTP_201_CREATED)

        invalid_json_payload = self._create_flag_with_properties(
            "invalid-json-flag",
            [{"key": "key", "value": "value", "type": "person"}],
            payloads={"true": "{invalid_json}"},
            expected_status=status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(invalid_json_payload.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(invalid_json_payload.json()["detail"], "Payload value is not valid JSON")

        non_string_payload = self._create_flag_with_properties(
            "non-string-json-flag",
            [{"key": "key", "value": "value", "type": "person"}],
            payloads={"true": {"key": "value"}},
            expected_status=status.HTTP_201_CREATED,
        )
        self.assertEqual(non_string_payload.status_code, status.HTTP_201_CREATED)

    def test_creating_feature_flag_with_behavioral_cohort(self):
        cohort_valid_for_ff = Cohort.objects.create(
            team=self.team,
            groups=[{"properties": [{"key": "$some_prop", "value": "nomatchihope", "type": "person"}]}],
            name="cohort1",
        )

        cohort_not_valid_for_ff = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "AND",
                    "values": [
                        {
                            "key": "$pageview",
                            "event_type": "events",
                            "time_value": 2,
                            "time_interval": "week",
                            "value": "performed_event_first_time",
                            "type": "behavioral",
                        },
                        {"key": "email", "value": "test@posthog.com", "type": "person"},
                    ],
                }
            },
            name="cohort2",
        )

        cohort_request = self._create_flag_with_properties(
            "cohort-flag",
            [{"key": "id", "type": "cohort", "value": cohort_not_valid_for_ff.id}],
            expected_status=status.HTTP_400_BAD_REQUEST,
        )

        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "behavioral_cohort_found",
                "detail": "Cohort 'cohort2' with filters on events cannot be used in feature flags.",
                "attr": "filters",
            },
            cohort_request.json(),
        )

        cohort_request = self._create_flag_with_properties(
            "cohort-flag",
            [{"key": "id", "type": "cohort", "value": cohort_valid_for_ff.id}],
            expected_status=status.HTTP_201_CREATED,
        )
        flag_id = cohort_request.json()["id"]
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{flag_id}",
            {
                "name": "Updated name",
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 65,
                            "properties": [
                                {
                                    "key": "id",
                                    "type": "cohort",
                                    "value": cohort_not_valid_for_ff.id,
                                }
                            ],
                        }
                    ]
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "behavioral_cohort_found",
                "detail": "Cohort 'cohort2' with filters on events cannot be used in feature flags.",
                "attr": "filters",
            },
            response.json(),
        )

    def test_creating_feature_flag_with_nested_behavioral_cohort(self):
        cohort_not_valid_for_ff = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "AND",
                    "values": [
                        {
                            "key": "$pageview",
                            "event_type": "events",
                            "time_value": 2,
                            "time_interval": "week",
                            "value": "performed_event_first_time",
                            "type": "behavioral",
                        },
                        {"key": "email", "value": "test@posthog.com", "type": "person"},
                    ],
                }
            },
            name="cohort-behavioural",
        )

        nested_cohort_not_valid_for_ff = Cohort.objects.create(
            team=self.team,
            groups=[
                {
                    "properties": [
                        {
                            "key": "id",
                            "value": cohort_not_valid_for_ff.pk,
                            "type": "cohort",
                        }
                    ]
                }
            ],
            name="cohort-not-behavioural",
        )

        cohort_request = self._create_flag_with_properties(
            "cohort-flag",
            [
                {
                    "key": "id",
                    "type": "cohort",
                    "value": nested_cohort_not_valid_for_ff.id,
                }
            ],
            expected_status=status.HTTP_400_BAD_REQUEST,
        )

        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "behavioral_cohort_found",
                "detail": "Cohort 'cohort-behavioural' with filters on events cannot be used in feature flags.",
                "attr": "filters",
            },
            cohort_request.json(),
        )

        cohort_request = self._create_flag_with_properties(
            "cohort-flag",
            [{"key": "id", "type": "cohort", "value": cohort_not_valid_for_ff.id}],
            expected_status=status.HTTP_400_BAD_REQUEST,
        )

        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "behavioral_cohort_found",
                "detail": "Cohort 'cohort-behavioural' with filters on events cannot be used in feature flags.",
                "attr": "filters",
            },
            cohort_request.json(),
        )

    def test_validation_group_properties(self):
        groups_request = self._create_flag_with_properties(
            "groups-flag",
            [
                {
                    "key": "industry",
                    "value": "finance",
                    "type": "group",
                    "group_type_index": 0,
                }
            ],
            aggregation_group_type_index=0,
        )
        self.assertEqual(groups_request.status_code, status.HTTP_201_CREATED)

        illegal_groups_request = self._create_flag_with_properties(
            "illegal-groups-flag",
            [
                {
                    "key": "industry",
                    "value": "finance",
                    "type": "group",
                    "group_type_index": 0,
                }
            ],
            aggregation_group_type_index=3,
            expected_status=status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            illegal_groups_request.json(),
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "Filters are not valid (can only use group properties)",
                "attr": "filters",
            },
        )

        person_request = self._create_flag_with_properties(
            "person-flag",
            [
                {
                    "key": "email",
                    "type": "person",
                    "value": "@posthog.com",
                    "operator": "icontains",
                }
            ],
            aggregation_group_type_index=0,
            expected_status=status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            person_request.json(),
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "Filters are not valid (can only use group properties)",
                "attr": "filters",
            },
        )

    def _create_flag_with_properties(
        self,
        name: str,
        properties,
        team_id: Optional[int] = None,
        expected_status: int = status.HTTP_201_CREATED,
        **kwargs,
    ):
        if team_id is None:
            team_id = self.team.id

        create_response = self.client.post(
            f"/api/projects/{team_id}/feature_flags/",
            data={
                "name": name,
                "key": name,
                "filters": {**kwargs, "groups": [{"properties": properties}]},
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, expected_status)
        return create_response

    def _get_feature_flag_activity(
        self,
        flag_id: Optional[int] = None,
        team_id: Optional[int] = None,
        expected_status: int = status.HTTP_200_OK,
    ):
        if team_id is None:
            team_id = self.team.id

        if flag_id:
            url = f"/api/projects/{team_id}/feature_flags/{flag_id}/activity"
        else:
            url = f"/api/projects/{team_id}/feature_flags/activity"

        activity = self.client.get(url)
        self.assertEqual(activity.status_code, expected_status)
        if activity.status_code == status.HTTP_404_NOT_FOUND:
            return None
        return activity.json()

    def assert_feature_flag_activity(self, flag_id: Optional[int], expected: list[dict]):
        activity_response = self._get_feature_flag_activity(flag_id)

        activity: list[dict] = activity_response["results"]
        self.maxDiff = None
        assert activity == expected

    def test_patch_api_as_form_data(self):
        another_feature_flag = FeatureFlag.objects.create(
            team=self.team,
            name="some feature",
            key="some-feature",
            created_by=self.user,
            filters={
                "groups": [{"properties": [], "rollout_percentage": 100}],
                "multivariate": None,
            },
            active=True,
        )

        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{another_feature_flag.pk}/",
            data="active=False&name=replaced",
            content_type="application/x-www-form-urlencoded",
        )

        self.assertEqual(response.status_code, 200)
        updated_flag = FeatureFlag.objects.get(pk=another_feature_flag.pk)
        self.assertEqual(updated_flag.active, False)
        self.assertEqual(updated_flag.name, "replaced")
        self.assertEqual(
            updated_flag.filters,
            {
                "groups": [{"properties": [], "rollout_percentage": 100}],
                "multivariate": None,
            },
        )

    def test_feature_flag_threshold(self):
        feature_flag = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [{"rollout_percentage": 65}],
                },
                "rollback_conditions": [
                    {
                        "threshold": 5000,
                        "threshold_metric": {
                            "insight": "trends",
                            "events": [{"order": 0, "id": "$pageview"}],
                            "properties": [
                                {
                                    "key": "$geoip_country_name",
                                    "type": "person",
                                    "value": ["france"],
                                    "operator": "exact",
                                }
                            ],
                        },
                        "operator": "lt",
                        "threshold_type": "insight",
                    }
                ],
                "auto-rollback": True,
            },
            format="json",
        ).json()

        self.assertEqual(len(feature_flag["rollback_conditions"]), 1)

    def test_feature_flag_can_edit(self):
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        self.assertEqual(
            (
                AvailableFeature.ROLE_BASED_ACCESS
                in [feature["key"] for feature in self.organization.available_product_features or []]
            ),
            False,
        )
        user_a = User.objects.create_and_join(self.organization, "a@potato.com", None)
        FeatureFlag.objects.create(team=self.team, created_by=user_a, key="blue_button")
        res = self.client.get(f"/api/projects/{self.team.id}/feature_flags/")
        self.assertEqual(res.json()["results"][0]["can_edit"], True)
        self.assertEqual(res.json()["results"][1]["can_edit"], True)

    def test_get_flags_dont_return_survey_targeting_flags(self):
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        survey = self.client.post(
            f"/api/projects/{self.team.id}/surveys/",
            data={
                "name": "Notebooks power users survey",
                "type": "popover",
                "questions": [
                    {
                        "type": "open",
                        "question": "What would you want to improve from notebooks?",
                    }
                ],
                "targeting_flag_filters": {
                    "groups": [
                        {
                            "variant": None,
                            "rollout_percentage": None,
                            "properties": [
                                {
                                    "key": "billing_plan",
                                    "value": ["cloud"],
                                    "operator": "exact",
                                    "type": "person",
                                }
                            ],
                        }
                    ]
                },
                "conditions": {"url": "https://app.posthog.com/notebooks"},
            },
            format="json",
        )
        assert FeatureFlag.objects.filter(id=survey.json()["targeting_flag"]["id"]).exists()

        flags_list = self.client.get(f"/api/projects/@current/feature_flags")
        response = flags_list.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["id"] is not survey.json()["targeting_flag"]["id"]

    def test_get_flags_with_active_and_created_by_id_filters(self):
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        another_user = User.objects.create(email="foo@bar.com")
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="blue_button")
        FeatureFlag.objects.create(team=self.team, created_by=another_user, key="orange_button", active=False)
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="green_button", active=False)

        filtered_flags_list = self.client.get(
            f"/api/projects/@current/feature_flags?created_by_id={self.user.id}&active=false"
        )
        response = filtered_flags_list.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == "green_button"

    def test_get_flags_with_type_filters(self):
        feature_flag = FeatureFlag.objects.create(team=self.team, created_by=self.user, key="red_button")
        Experiment.objects.create(
            team=self.team, created_by=self.user, name="Experiment 1", feature_flag_id=feature_flag.id
        )
        FeatureFlag.objects.create(
            team=self.team,
            created_by=self.user,
            key="purple_button",
            filters={"multivariate": {"variants": [{"foo": "bar"}]}},
        )

        filtered_flags_list_boolean = self.client.get(f"/api/projects/@current/feature_flags?type=boolean")
        response = filtered_flags_list_boolean.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == feature_flag.key

        filtered_flags_list_multivariant = self.client.get(f"/api/projects/@current/feature_flags?type=multivariant")
        response = filtered_flags_list_multivariant.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == "purple_button"

        filtered_flags_list_experiment = self.client.get(f"/api/projects/@current/feature_flags?type=experiment")
        response = filtered_flags_list_experiment.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == feature_flag.key

    def test_get_flags_with_search(self):
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="blue_search_term_button")
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="green_search_term_button", active=False)

        filtered_flags_list = self.client.get(f"/api/projects/@current/feature_flags?active=true&search=search_term")
        response = filtered_flags_list.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == "blue_search_term_button"

    def test_get_flags_with_stale_filter(self):
        # Create a stale flag (100% rollout with no properties and 30+ days old)
        with freeze_time("2024-01-01"):
            FeatureFlag.objects.create(
                team=self.team,
                created_by=self.user,
                key="stale_flag",
                active=True,
                filters={"groups": [{"rollout_percentage": 100, "properties": []}]},
            )

        # Create a non-stale flag (100% rollout but recent)
        FeatureFlag.objects.create(
            team=self.team,
            created_by=self.user,
            key="recent_flag",
            active=True,
            filters={"groups": [{"rollout_percentage": 100, "properties": []}]},
        )

        # Create another non-stale flag (old but not 100% rollout)
        with freeze_time("2024-01-01"):
            FeatureFlag.objects.create(
                team=self.team,
                created_by=self.user,
                key="partial_flag",
                active=True,
                filters={"groups": [{"rollout_percentage": 50, "properties": []}]},
            )

        # Create a non-stale flag (100% rollout but has properties)
        with freeze_time("2024-01-01"):
            FeatureFlag.objects.create(
                team=self.team,
                created_by=self.user,
                key="filtered_flag",
                active=True,
                filters={
                    "groups": [
                        {
                            "rollout_percentage": 100,
                            "properties": [
                                {"key": "email", "value": "test@example.com", "operator": "exact", "type": "person"}
                            ],
                        }
                    ]
                },
            )

        # Test filtering by stale status
        filtered_flags_list = self.client.get(f"/api/projects/@current/feature_flags?active=STALE")
        response = filtered_flags_list.json()

        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == "stale_flag"
        assert response["results"][0]["status"] == "STALE"

    def test_get_flags_with_stale_filter_multivariate(self):
        # Create a stale multivariate flag
        with freeze_time("2023-01-01"):
            FeatureFlag.objects.create(
                team=self.team,
                created_by=self.user,
                key="stale_multivariate",
                active=True,
                filters={
                    "groups": [{"rollout_percentage": 100, "properties": []}],
                    "multivariate": {
                        "variants": [
                            {"key": "test", "rollout_percentage": 100},
                        ],
                        "release_percentage": 100,
                    },
                },
            )

        # Create a non-stale multivariate flag (no variant at 100%)
        with freeze_time("2024-01-01"):
            FeatureFlag.objects.create(
                team=self.team,
                created_by=self.user,
                key="active_multivariate",
                active=True,
                filters={
                    "groups": [{"rollout_percentage": 50, "properties": []}],
                    "multivariate": {
                        "variants": [
                            {"key": "test", "rollout_percentage": 30},
                            {"key": "test2", "rollout_percentage": 70},
                        ]
                    },
                },
            )

        # Test filtering by stale status
        filtered_flags_list = self.client.get(f"/api/projects/@current/feature_flags?active=STALE")
        response = filtered_flags_list.json()

        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == "stale_multivariate"
        assert response["results"][0]["status"] == "STALE"

    def test_get_flags_with_evaluation_runtime_filter(self):
        # Create flags with different evaluation runtimes
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="server_flag", evaluation_runtime="server")
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="client_flag", evaluation_runtime="client")
        FeatureFlag.objects.create(team=self.team, created_by=self.user, key="both_flag", evaluation_runtime="both")

        # Test filtering by server environment
        filtered_flags_list = self.client.get(f"/api/projects/@current/feature_flags?evaluation_runtime=server")
        response = filtered_flags_list.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == "server_flag"

        # Test filtering by client environment
        filtered_flags_list = self.client.get(f"/api/projects/@current/feature_flags?evaluation_runtime=client")
        response = filtered_flags_list.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == "client_flag"

        # Test filtering by both environment
        filtered_flags_list = self.client.get(f"/api/projects/@current/feature_flags?evaluation_runtime=both")
        response = filtered_flags_list.json()
        assert len(response["results"]) == 1
        assert response["results"][0]["key"] == "both_flag"

    def test_flag_is_cached_on_create_and_update(self):
        # Ensure empty feature flag list
        FeatureFlag.objects.all().delete()

        feature_flag = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "name": "Beta feature",
                "key": "beta-feature",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [{"rollout_percentage": 65}],
                },
            },
            format="json",
        ).json()

        flags = get_feature_flags_for_team_in_cache(self.team.id)

        assert flags is not None
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].id, feature_flag["id"])
        self.assertEqual(flags[0].key, "beta-feature")
        self.assertEqual(flags[0].name, "Beta feature")

        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{feature_flag['id']}",
            {"name": "XYZ", "key": "red_button"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        flags = get_feature_flags_for_team_in_cache(self.team.id)

        assert flags is not None
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].id, feature_flag["id"])
        self.assertEqual(flags[0].key, "red_button")
        self.assertEqual(flags[0].name, "XYZ")

        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{feature_flag['id']}",
            {"deleted": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        flags = get_feature_flags_for_team_in_cache(self.team.id)

        assert flags is not None
        self.assertEqual(len(flags), 0)

    @patch("posthog.api.feature_flag.FeatureFlagThrottle.rate", new="7/minute")
    @patch("posthog.rate_limit.BurstRateThrottle.rate", new="5/minute")
    @patch("posthog.rate_limit.statsd.incr")
    @patch("posthog.rate_limit.is_rate_limit_enabled", return_value=True)
    def test_rate_limits_for_local_evaluation_are_independent(self, rate_limit_enabled_mock, incr_mock):
        personal_api_key = generate_random_token_personal()
        PersonalAPIKey.objects.create(label="X", user=self.user, secure_value=hash_key_value(personal_api_key))

        for _ in range(5):
            response = self.client.get(
                f"/api/projects/{self.team.pk}/feature_flags",
                HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Call to flags gets rate limited
        response = self.client.get(
            f"/api/projects/{self.team.pk}/feature_flags",
            HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
        )
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(
            len([1 for name, args, kwargs in incr_mock.mock_calls if args[0] == "rate_limit_exceeded"]),
            1,
        )
        incr_mock.assert_any_call(
            "rate_limit_exceeded",
            tags={
                "team_id": self.team.pk,
                "scope": "burst",
                "rate": "5/minute",
                "path": f"/api/projects/TEAM_ID/feature_flags",
                "hashed_personal_api_key": hash_key_value(personal_api_key),
            },
        )

        incr_mock.reset_mock()

        # but not call to local evaluation
        for _ in range(7):
            response = self.client.get(
                f"/api/feature_flag/local_evaluation",
                HTTP_AUTHORIZATION=f"Bearer {personal_api_key}",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            len([1 for name, args, kwargs in incr_mock.mock_calls if args[0] == "rate_limit_exceeded"]),
            0,
        )

    def test_feature_flag_dashboard(self):
        another_feature_flag = FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=50,
            name="some feature",
            key="some-feature",
            created_by=self.user,
        )
        dashboard = Dashboard.objects.create(team=self.team, name="private dashboard", created_by=self.user)
        relationship = FeatureFlagDashboards.objects.create(
            feature_flag=another_feature_flag, dashboard_id=dashboard.pk
        )

        response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/" + str(another_feature_flag.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_json = response.json()

        self.assertEqual(len(response_json["analytics_dashboards"]), 1)

        # check deleting the dashboard doesn't delete flag, but deletes the relationship
        dashboard.delete()
        another_feature_flag.refresh_from_db()

        with self.assertRaises(FeatureFlagDashboards.DoesNotExist):
            relationship.refresh_from_db()

    def test_feature_flag_dashboard_patch(self):
        another_feature_flag = FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=50,
            name="some feature",
            key="some-feature",
            created_by=self.user,
        )
        dashboard = Dashboard.objects.create(team=self.team, name="private dashboard", created_by=self.user)
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/" + str(another_feature_flag.pk),
            {"analytics_dashboards": [dashboard.pk]},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(f"/api/projects/{self.team.id}/feature_flags/" + str(another_feature_flag.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_json = response.json()

        self.assertEqual(len(response_json["analytics_dashboards"]), 1)

    def test_feature_flag_dashboard_already_exists(self):
        another_feature_flag = FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=50,
            name="some feature",
            key="some-feature",
            created_by=self.user,
        )
        dashboard = Dashboard.objects.create(team=self.team, name="private dashboard", created_by=self.user)
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/" + str(another_feature_flag.pk),
            {"analytics_dashboards": [dashboard.pk]},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/" + str(another_feature_flag.pk),
            {"analytics_dashboards": [dashboard.pk]},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_json = response.json()

        self.assertEqual(len(response_json["analytics_dashboards"]), 1)

    @freeze_time("2021-01-01")
    @snapshot_clickhouse_queries
    def test_creating_static_cohort(self):
        flag = FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=100,
            filters={
                "groups": [{"properties": [{"key": "key", "value": "value", "type": "person"}]}],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature",
            created_by=self.user,
        )

        _create_person(
            team=self.team,
            distinct_ids=[f"person1"],
            properties={"key": "value"},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person2"],
            properties={"key": "value2"},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person3"],
            properties={"key2": "value3"},
        )
        flush_persons_and_events()

        with (
            snapshot_postgres_queries_context(self),
            self.settings(
                CELERY_TASK_ALWAYS_EAGER=True, PERSON_ON_EVENTS_OVERRIDE=False, PERSON_ON_EVENTS_V2_OVERRIDE=False
            ),
        ):
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/{flag.id}/create_static_cohort_for_flag",
                {},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # fires an async task for computation, but celery runs sync in tests
        cohort_id = response.json()["cohort"]["id"]
        cohort = Cohort.objects.get(id=cohort_id)
        self.assertEqual(cohort.name, "Users with feature flag some-feature enabled at 2021-01-01 00:00:00")
        self.assertEqual(cohort.count, 1)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 1, response)

    def test_cant_update_early_access_flag_with_group(self):
        feature_flag = FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=100,
            filters={
                "aggregation_group_type_index": None,
                "groups": [{"properties": [], "rollout_percentage": None}],
            },
            name="some feature",
            key="some-feature",
            created_by=self.user,
        )

        EarlyAccessFeature.objects.create(
            team=self.team,
            name="earlyAccessFeature",
            description="early access feature",
            stage="alpha",
            feature_flag=feature_flag,
        )

        update_data = {
            "filters": {
                "aggregation_group_type_index": 2,
                "groups": [{"properties": [], "rollout_percentage": 100}],
            }
        }
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{feature_flag.id}/", update_data, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "Cannot change this flag to a group-based when linked to an Early Access Feature.",
            },
            response.json(),
        )

    def test_cant_create_flag_with_data_that_fails_to_query(self):
        Person.objects.create(
            distinct_ids=["123"],
            team=self.team,
            properties={"email": "x y z"},
        )
        Person.objects.create(
            distinct_ids=["456"],
            team=self.team,
            properties={"email": "2.3.999"},
        )

        # Only snapshot flag evaluation queries
        with snapshot_postgres_queries_context(self, custom_query_matcher=lambda query: "posthog_person" in query):
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags",
                {
                    "name": "Beta feature",
                    "key": "beta-x",
                    "filters": {
                        "groups": [
                            {
                                "rollout_percentage": 65,
                                "properties": [
                                    {
                                        "key": "email",
                                        "type": "person",
                                        "value": "2.3.9{0-9}{1}",
                                        "operator": "regex",
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(
                response.json(),
                {
                    "type": "validation_error",
                    "code": "invalid_input",
                    "detail": "Can't evaluate flag - please check release conditions",
                    "attr": None,
                },
            )

    def test_cant_create_flag_with_group_data_that_fails_to_query(self):
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="xyz", group_type_index=1
        )

        for i in range(5):
            create_group(
                team_id=self.team.pk,
                group_type_index=1,
                group_key=f"xyz:{i}",
                properties={"industry": f"{i}", "email": "2.3.4445"},
            )

        # Only snapshot flag evaluation queries
        with snapshot_postgres_queries_context(self, custom_query_matcher=lambda query: "posthog_group" in query):
            # Test group flag with invalid regex
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags",
                {
                    "name": "Beta feature",
                    "key": "beta-x",
                    "filters": {
                        "aggregation_group_type_index": 1,
                        "groups": [
                            {
                                "rollout_percentage": 65,
                                "properties": [
                                    {
                                        "key": "email",
                                        "type": "group",
                                        "group_type_index": 1,
                                        "value": "2.3.9{0-9}{1 ef}",
                                        "operator": "regex",
                                    }
                                ],
                            }
                        ],
                    },
                },
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "Can't evaluate flag - please check release conditions",
                "attr": None,
            },
        )

    def test_feature_flag_includes_cohort_names(self):
        cohort = Cohort.objects.create(
            team=self.team,
            name="test_cohort",
            groups=[{"properties": [{"key": "email", "value": "@posthog.com", "type": "person"}]}],
        )
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            {
                "name": "Alpha feature",
                "key": "alpha-feature",
                "filters": {
                    "groups": [{"properties": [{"key": "id", "type": "cohort", "value": cohort.pk}]}],
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Get the flag
        response = self.client.get(
            f"/api/projects/{self.team.id}/feature_flags/{response.json()['id']}/",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json()["filters"]["groups"][0]["properties"][0],
            {
                "key": "id",
                "type": "cohort",
                "value": cohort.pk,
                "cohort_name": "test_cohort",
            },
        )

    def test_create_feature_flag_in_specific_folder(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/",
            data={
                "key": "my-test-flag-in-folder",
                "name": "Test Flag in Folder",
                "filters": {"groups": [{"properties": [], "rollout_percentage": 50}]},
                "_create_in_folder": "Special Folder/Flags",
            },
            format="json",
        )
        assert response.status_code == 201, response.json()
        flag_id = response.json()["id"]
        assert flag_id is not None

        from posthog.models.file_system.file_system import FileSystem

        fs_entry = FileSystem.objects.filter(team=self.team, ref=str(flag_id), type="feature_flag").first()
        assert fs_entry, "No FileSystem entry found for this feature flag."
        assert (
            "Special Folder/Flags" in fs_entry.path
        ), f"Expected 'Special Folder/Flags' in path, got: '{fs_entry.path}'"

    @patch("posthog.api.feature_flag.report_user_action")
    def test_updating_feature_flag_key_updates_super_groups(self, mock_capture):
        # Create a feature flag with super_groups
        feature_flag = FeatureFlag.objects.create(
            team=self.team,
            created_by=self.user,
            key="old-key",
            name="Test Flag",
            filters={
                "groups": [{"properties": [], "rollout_percentage": 0}],
                "super_groups": [
                    {
                        "properties": [
                            {
                                "key": "$feature_enrollment/old-key",
                                "type": "person",
                                "value": ["true"],
                                "operator": "exact",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ],
            },
        )

        # Update the feature flag key
        response = self.client.patch(
            f"/api/projects/{self.team.id}/feature_flags/{feature_flag.id}",
            {"key": "new-key"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Refresh the feature flag from the database
        feature_flag.refresh_from_db()

        # Verify the super_groups were updated
        self.assertEqual(feature_flag.filters["super_groups"][0]["properties"][0]["key"], "$feature_enrollment/new-key")

        # Verify the old key is not present
        self.assertNotIn("$feature_enrollment/old-key", str(feature_flag.filters))

    def test_feature_flag_experiment_set(self):
        # Create a feature flag
        feature_flag = FeatureFlag.objects.create(
            team=self.team,
            created_by=self.user,
            key="test-flag",
            name="Test Flag",
            filters={"groups": [{"properties": [], "rollout_percentage": 100}]},
        )

        # Initially, experiment_set should be empty
        response = self.client.get(f"/api/projects/@current/feature_flags/{feature_flag.id}")
        assert response.status_code == 200
        assert response.json()["experiment_set"] == []

        # Create an active experiment linked to the flag
        experiment = Experiment.objects.create(
            team=self.team,
            created_by=self.user,
            name="Test Experiment",
            feature_flag=feature_flag,
            start_date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        )

        # experiment_set should now include the experiment ID
        response = self.client.get(f"/api/projects/@current/feature_flags/{feature_flag.id}")
        assert response.status_code == 200
        assert response.json()["experiment_set"] == [experiment.id]

        # Create a deleted experiment - should not be included
        experiment2 = Experiment.objects.create(
            team=self.team,
            created_by=self.user,
            name="Another Experiment",
            feature_flag=feature_flag,
            start_date=datetime(2024, 1, 1, 12, 1, 0, tzinfo=UTC),
        )

        # experiment_set should include both experiments
        response = self.client.get(f"/api/projects/@current/feature_flags/{feature_flag.id}")
        assert response.status_code == 200
        assert response.json()["experiment_set"] == [experiment.id, experiment2.id]

        # Delete the active experiments
        experiment.deleted = True
        experiment.save()
        experiment2.deleted = True
        experiment2.save()

        # experiment_set should now be empty again
        response = self.client.get(f"/api/projects/@current/feature_flags/{feature_flag.id}")
        assert response.status_code == 200
        assert response.json()["experiment_set"] == []

    def test_bulk_keys_valid_ids(self):
        """Test that valid IDs return correct key mapping"""
        # Create test flags
        flag1 = FeatureFlag.objects.create(key="test-flag-1", name="Test Flag 1", team=self.team, created_by=self.user)
        flag2 = FeatureFlag.objects.create(key="test-flag-2", name="Test Flag 2", team=self.team, created_by=self.user)
        flag3 = FeatureFlag.objects.create(key="test-flag-3", name="Test Flag 3", team=self.team, created_by=self.user)

        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {"ids": [flag1.id, flag2.id, flag3.id]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "keys" in data
        assert data["keys"] == {
            str(flag1.id): "test-flag-1",
            str(flag2.id): "test-flag-2",
            str(flag3.id): "test-flag-3",
        }

    def test_bulk_keys_empty_list(self):
        """Test that empty ID list returns empty keys object"""
        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {"ids": []},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data == {"keys": {}}

    def test_bulk_keys_invalid_ids(self):
        """Test that invalid IDs (non-integers) return error"""
        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {"ids": ["invalid", "not-a-number"]},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        data = response.json()
        assert "error" in data
        assert "Invalid flag IDs provided" in data["error"]

    def test_bulk_keys_mixed_valid_invalid_ids(self):
        """Test that mixed valid/invalid IDs filter out invalid ones"""
        flag1 = FeatureFlag.objects.create(key="test-flag-1", name="Test Flag 1", team=self.team, created_by=self.user)

        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {"ids": [flag1.id, "invalid", 99999]},  # valid ID, invalid string, non-existent ID
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "keys" in data
        assert data["keys"] == {str(flag1.id): "test-flag-1"}
        assert "warning" in data
        assert "Invalid flag IDs ignored: ['invalid']" in data["warning"]

    def test_bulk_keys_nonexistent_ids(self):
        """Test that non-existent flag IDs are filtered out"""
        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {"ids": [99999, 88888]},  # Non-existent IDs
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data == {"keys": {}}

    def test_bulk_keys_team_isolation(self):
        """Test that flags from other teams are not returned"""
        # Create flag in current team
        flag1 = FeatureFlag.objects.create(
            key="current-team-flag", name="Current Team Flag", team=self.team, created_by=self.user
        )

        # Create another team and flag
        other_user = User.objects.create_user(email="other@test.com", password="password", first_name="Other")
        other_organization, _, other_team = Organization.objects.bootstrap(other_user)
        flag2 = FeatureFlag.objects.create(
            key="other-team-flag", name="Other Team Flag", team=other_team, created_by=other_user
        )

        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {"ids": [flag1.id, flag2.id]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "keys" in data
        # Should only return flag from current team
        assert data["keys"] == {str(flag1.id): "current-team-flag"}

    def test_bulk_keys_deleted_flags(self):
        """Test that deleted flags are not returned"""
        flag1 = FeatureFlag.objects.create(key="active-flag", name="Active Flag", team=self.team, created_by=self.user)
        flag2 = FeatureFlag.objects.create(
            key="deleted-flag", name="Deleted Flag", team=self.team, created_by=self.user, deleted=True
        )

        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {"ids": [flag1.id, flag2.id]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "keys" in data
        # Should only return non-deleted flag
        assert data["keys"] == {str(flag1.id): "active-flag"}

    def test_bulk_keys_no_ids_param(self):
        """Test that missing 'ids' parameter returns empty keys object"""
        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {},  # No 'ids' parameter
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data == {"keys": {}}

    def test_bulk_keys_string_ids(self):
        """Test that string representations of valid IDs work"""
        flag1 = FeatureFlag.objects.create(key="test-flag-1", name="Test Flag 1", team=self.team, created_by=self.user)

        response = self.client.post(
            f"/api/projects/@current/feature_flags/bulk_keys/",
            {"ids": [str(flag1.id)]},  # String ID instead of integer
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "keys" in data
        assert data["keys"] == {str(flag1.id): "test-flag-1"}


class TestCohortGenerationForFeatureFlag(APIBaseTest, ClickhouseTestMixin):
    def test_creating_static_cohort_with_deleted_flag(self):
        FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=100,
            filters={
                "groups": [{"properties": [{"key": "key", "value": "value", "type": "person"}]}],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature",
            created_by=self.user,
            deleted=True,
        )

        _create_person(
            team=self.team,
            distinct_ids=[f"person1"],
            properties={"key": "value"},
        )
        flush_persons_and_events()

        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        with self.assertNumQueries(2):
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature", self.team.pk)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        # don't even try inserting anything, because invalid flag, so None instead of 0
        self.assertEqual(cohort.count, None)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 0, response)

    def test_creating_static_cohort_with_inactive_flag(self):
        FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=100,
            filters={
                "groups": [{"properties": [{"key": "key", "value": "value", "type": "person"}]}],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature2",
            created_by=self.user,
            active=False,
        )

        _create_person(
            team=self.team,
            distinct_ids=[f"person1"],
            properties={"key": "value"},
        )
        flush_persons_and_events()

        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        with self.assertNumQueries(2):
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature2", self.team.pk)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        # don't even try inserting anything, because invalid flag, so None instead of 0
        self.assertEqual(cohort.count, None)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 0, response)

    @freeze_time("2021-01-01")
    def test_creating_static_cohort_with_group_flag(self):
        FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=100,
            filters={
                "groups": [{"properties": [{"key": "key", "value": "value", "type": "group", "group_type_index": 1}]}],
                "multivariate": None,
                "aggregation_group_type_index": 1,
            },
            name="some feature",
            key="some-feature3",
            created_by=self.user,
        )

        _create_person(
            team=self.team,
            distinct_ids=[f"person1"],
            properties={"key": "value"},
        )
        flush_persons_and_events()

        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        with self.assertNumQueries(2):
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature3", self.team.pk)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        # don't even try inserting anything, because invalid flag, so None instead of 0
        self.assertEqual(cohort.count, None)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 0, response)

    def test_creating_static_cohort_with_no_person_distinct_ids(self):
        FeatureFlag.objects.create(
            team=self.team,
            rollout_percentage=100,
            filters={
                "groups": [{"properties": [], "rollout_percentage": 100}],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature2",
            created_by=self.user,
        )

        Person.objects.create(team=self.team)

        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        with self.assertNumQueries(6):
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature2", self.team.pk)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        # don't even try inserting anything, because invalid flag, so None instead of 0
        self.assertEqual(cohort.count, None)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 0, response)

    def test_creating_static_cohort_with_non_existing_flag(self):
        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        with self.assertNumQueries(2):
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature2", self.team.pk)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        # don't even try inserting anything, because invalid flag, so None instead of 0
        self.assertEqual(cohort.count, None)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 0, response)

    def test_creating_static_cohort_with_experience_continuity_flag(self):
        FeatureFlag.objects.create(
            team=self.team,
            filters={
                "groups": [
                    {"properties": [{"key": "key", "value": "value", "type": "person"}], "rollout_percentage": 50}
                ],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature2",
            created_by=self.user,
            ensure_experience_continuity=True,
        )

        p1 = _create_person(team=self.team, distinct_ids=[f"person1"], properties={"key": "value"}, immediate=True)
        _create_person(
            team=self.team,
            distinct_ids=[f"person2"],
            properties={"key": "value"},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person3"],
            properties={"key": "value"},
        )
        flush_persons_and_events()

        FeatureFlagHashKeyOverride.objects.create(
            feature_flag_key="some-feature2",
            person=p1,
            team=self.team,
            hash_key="123",
        )

        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        # TODO: Ensure server-side cursors are disabled, since in production we use this with pgbouncer
        with snapshot_postgres_queries_context(self), self.assertNumQueries(16):
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature2", self.team.pk)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        self.assertEqual(cohort.count, 1)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 1, response)

    def test_creating_static_cohort_iterator(self):
        FeatureFlag.objects.create(
            team=self.team,
            filters={
                "groups": [
                    {"properties": [{"key": "key", "value": "value", "type": "person"}], "rollout_percentage": 100}
                ],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature2",
            created_by=self.user,
        )

        _create_person(
            team=self.team,
            distinct_ids=[f"person1"],
            properties={"key": "value"},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person2"],
            properties={"key": "value"},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person3"],
            properties={"key": "value"},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person4"],
            properties={"key": "valuu3"},
        )
        flush_persons_and_events()

        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        # Extra queries because each batch adds its own queries
        with snapshot_postgres_queries_context(self), self.assertNumQueries(21):
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature2", self.team.pk, batchsize=2)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        self.assertEqual(cohort.count, 3)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 3, response)

        # if the batch is big enough, it's fewer queries
        with self.assertNumQueries(13):
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature2", self.team.pk, batchsize=10)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        self.assertEqual(cohort.count, 3)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 3, response)

    def test_creating_static_cohort_with_default_person_properties_adjustment(self):
        FeatureFlag.objects.create(
            team=self.team,
            filters={
                "groups": [
                    {
                        "properties": [{"key": "key", "value": "value", "type": "person", "operator": "icontains"}],
                        "rollout_percentage": 100,
                    }
                ],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature2",
            created_by=self.user,
            ensure_experience_continuity=False,
        )
        FeatureFlag.objects.create(
            team=self.team,
            filters={
                "groups": [
                    {
                        "properties": [{"key": "key", "value": "value", "type": "person", "operator": "is_set"}],
                        "rollout_percentage": 100,
                    }
                ],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature-new",
            created_by=self.user,
            ensure_experience_continuity=False,
        )

        _create_person(team=self.team, distinct_ids=[f"person1"], properties={"key": "value"})
        _create_person(
            team=self.team,
            distinct_ids=[f"person2"],
            properties={"key": "vaalue"},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person3"],
            properties={"key22": "value"},
        )
        flush_persons_and_events()

        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        with snapshot_postgres_queries_context(self), self.assertNumQueries(13):
            # no queries to evaluate flags, because all evaluated using override properties
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature2", self.team.pk)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        self.assertEqual(cohort.count, 1)

        response = self.client.get(f"/api/cohort/{cohort.pk}/persons")
        self.assertEqual(len(response.json()["results"]), 1, response)

        cohort2 = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort2",
        )

        with snapshot_postgres_queries_context(self), self.assertNumQueries(13):
            # person3 doesn't match filter conditions so is pre-filtered out
            get_cohort_actors_for_feature_flag(cohort2.pk, "some-feature-new", self.team.pk)

        cohort2.refresh_from_db()
        self.assertEqual(cohort2.name, "some cohort2")
        self.assertEqual(cohort2.count, 2)

    def test_creating_static_cohort_with_cohort_flag_adds_cohort_props_as_default_too(self):
        cohort_nested = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {"key": "does-not-exist", "value": "none", "type": "person"},
                            ],
                        }
                    ],
                }
            },
        )
        cohort_static = Cohort.objects.create(
            team=self.team,
            is_static=True,
        )
        cohort_existing = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {"key": "group", "value": "none", "type": "person"},
                                {"key": "group2", "value": [1, 2, 3], "type": "person"},
                                {"key": "id", "value": cohort_static.pk, "type": "cohort"},
                                {"key": "id", "value": cohort_nested.pk, "type": "cohort"},
                            ],
                        }
                    ],
                }
            },
            name="cohort1",
        )
        FeatureFlag.objects.create(
            team=self.team,
            filters={
                "groups": [
                    {
                        "properties": [{"key": "id", "value": cohort_existing.pk, "type": "cohort"}],
                        "rollout_percentage": 100,
                    },
                    {"properties": [{"key": "key", "value": "value", "type": "person"}], "rollout_percentage": 100},
                ],
                "multivariate": None,
            },
            name="some feature",
            key="some-feature-new",
            created_by=self.user,
            ensure_experience_continuity=False,
        )

        _create_person(team=self.team, distinct_ids=[f"person1"], properties={"key": "value"})
        _create_person(
            team=self.team,
            distinct_ids=[f"person2"],
            properties={"group": "none"},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person3"],
            properties={"key22": "value", "group2": 2},
        )
        _create_person(
            team=self.team,
            distinct_ids=[f"person4"],
            properties={},
        )
        flush_persons_and_events()

        cohort_static.insert_users_by_list([f"person4"])

        cohort = Cohort.objects.create(
            team=self.team,
            is_static=True,
            name="some cohort",
        )

        with snapshot_postgres_queries_context(self), self.assertNumQueries(30):
            # forced to evaluate flags by going to db, because cohorts need db query to evaluate
            get_cohort_actors_for_feature_flag(cohort.pk, "some-feature-new", self.team.pk)

        cohort.refresh_from_db()
        self.assertEqual(cohort.name, "some cohort")
        self.assertEqual(cohort.count, 4)


class TestBlastRadius(ClickhouseTestMixin, APIBaseTest):
    @snapshot_clickhouse_queries
    def test_user_blast_radius(self):
        for i in range(10):
            _create_person(
                team_id=self.team.pk,
                distinct_ids=[f"person{i}"],
                properties={"group": f"{i}"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {
                            "key": "group",
                            "type": "person",
                            "value": [0, 1, 2, 3],
                            "operator": "exact",
                        }
                    ],
                    "rollout_percentage": 25,
                }
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 4, "total_users": 10}, response_json)

    @freeze_time("2024-01-11")
    def test_user_blast_radius_with_relative_date_filters(self):
        for i in range(8):
            _create_person(
                team_id=self.team.pk,
                distinct_ids=[f"person{i}"],
                properties={"group": f"{i}", "created_at": f"2023-0{i + 1}-04"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {
                            "key": "created_at",
                            "type": "person",
                            "value": "-10m",
                            "operator": "is_date_before",
                        }
                    ],
                    "rollout_percentage": 100,
                }
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 3, "total_users": 8}, response_json)

    def test_user_blast_radius_with_zero_users(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {
                            "key": "group",
                            "type": "person",
                            "value": [0, 1, 2, 3],
                            "operator": "exact",
                        }
                    ],
                    "rollout_percentage": 25,
                }
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 0, "total_users": 0}, response_json)

    def test_user_blast_radius_with_zero_selected_users(self):
        for i in range(5):
            _create_person(
                team_id=self.team.pk,
                distinct_ids=[f"person{i}"],
                properties={"group": f"{i}"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {
                            "key": "group",
                            "type": "person",
                            "value": [8],
                            "operator": "exact",
                        }
                    ],
                    "rollout_percentage": 25,
                }
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 0, "total_users": 5}, response_json)

    def test_user_blast_radius_with_all_selected_users(self):
        for i in range(5):
            _create_person(
                team_id=self.team.pk,
                distinct_ids=[f"person{i}"],
                properties={"group": f"{i}"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {"condition": {"properties": [], "rollout_percentage": 100}},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 5, "total_users": 5}, response_json)

    @snapshot_clickhouse_queries
    def test_user_blast_radius_with_single_cohort(self):
        # Just to shake things up, we're using integers for the group property
        for i in range(10):
            _create_person(
                team_id=self.team.pk,
                distinct_ids=[f"person{i}"],
                properties={"group": i},
            )

        cohort1 = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {"key": "group", "value": "none", "type": "person"},
                                {"key": "group", "value": ["1", "2", "3"], "type": "person"},
                            ],
                        }
                    ],
                }
            },
            name="cohort1",
        )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [{"key": "id", "type": "cohort", "value": cohort1.pk}],
                    "rollout_percentage": 50,
                }
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 3, "total_users": 10}, response_json)

        # test the same with precalculated cohort. Snapshots shouldn't have group property filter
        cohort1.calculate_people_ch(pending_version=0)

        with self.settings(USE_PRECALCULATED_CH_COHORT_PEOPLE=True):
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
                {
                    "condition": {
                        "properties": [{"key": "id", "type": "cohort", "value": cohort1.pk}],
                        "rollout_percentage": 50,
                    }
                },
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            response_json = response.json()
            self.assertDictContainsSubset({"users_affected": 3, "total_users": 10}, response_json)

    @snapshot_clickhouse_queries
    def test_user_blast_radius_with_multiple_precalculated_cohorts(self):
        for i in range(10):
            _create_person(
                team_id=self.team.pk,
                distinct_ids=[f"person{i}"],
                properties={"group": f"{i}"},
            )

        cohort1 = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {"key": "group", "value": "none", "type": "person"},
                                {"key": "group", "value": ["1", "2", "3"], "type": "person"},
                            ],
                        }
                    ],
                }
            },
            name="cohort1",
        )

        cohort2 = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {
                                    "key": "group",
                                    "value": ["1", "2", "4", "5", "6"],
                                    "type": "person",
                                },
                            ],
                        }
                    ],
                }
            },
            name="cohort2",
        )

        # converts to precalculated-cohort due to simplify filters
        cohort1.calculate_people_ch(pending_version=0)
        cohort2.calculate_people_ch(pending_version=0)

        with self.settings(USE_PRECALCULATED_CH_COHORT_PEOPLE=True):
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
                {
                    "condition": {
                        "properties": [
                            {"key": "id", "type": "cohort", "value": cohort1.pk},
                            {"key": "id", "type": "cohort", "value": cohort2.pk},
                        ],
                        "rollout_percentage": 50,
                    }
                },
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            response_json = response.json()
            self.assertDictContainsSubset({"users_affected": 2, "total_users": 10}, response_json)

    @snapshot_clickhouse_queries
    def test_user_blast_radius_with_multiple_static_cohorts(self):
        for i in range(10):
            _create_person(
                team_id=self.team.pk,
                distinct_ids=[f"person{i}"],
                properties={"group": f"{i}"},
            )

        cohort1 = Cohort.objects.create(team=self.team, groups=[], is_static=True, last_calculation=now())
        cohort1.insert_users_by_list(["person0", "person1", "person2"])

        cohort2 = Cohort.objects.create(
            team=self.team,
            filters={
                "properties": {
                    "type": "OR",
                    "values": [
                        {
                            "type": "OR",
                            "values": [
                                {
                                    "key": "group",
                                    "value": ["1", "2", "4", "5", "6"],
                                    "type": "person",
                                },
                            ],
                        }
                    ],
                }
            },
            name="cohort2",
        )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {"key": "id", "type": "cohort", "value": cohort1.pk},
                        {"key": "id", "type": "cohort", "value": cohort2.pk},
                    ],
                    "rollout_percentage": 50,
                }
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 2, "total_users": 10}, response_json)

        cohort1.calculate_people_ch(pending_version=0)
        # converts to precalculated-cohort due to simplify filters
        cohort2.calculate_people_ch(pending_version=0)

        with self.settings(USE_PRECALCULATED_CH_COHORT_PEOPLE=True):
            response = self.client.post(
                f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
                {
                    "condition": {
                        "properties": [
                            {"key": "id", "type": "cohort", "value": cohort1.pk},
                            {"key": "id", "type": "cohort", "value": cohort2.pk},
                        ],
                        "rollout_percentage": 50,
                    }
                },
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            response_json = response.json()
            self.assertDictContainsSubset({"users_affected": 2, "total_users": 10}, response_json)

    @snapshot_clickhouse_queries
    def test_user_blast_radius_with_groups(self):
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )

        for i in range(10):
            create_group(
                team_id=self.team.pk,
                group_type_index=0,
                group_key=f"org:{i}",
                properties={"industry": f"{i}"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {
                            "key": "industry",
                            "type": "group",
                            "value": [0, 1, 2, 3],
                            "operator": "exact",
                            "group_type_index": 0,
                        }
                    ],
                    "rollout_percentage": 25,
                },
                "group_type_index": 0,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 4, "total_users": 10}, response_json)

    def test_user_blast_radius_with_groups_zero_selected(self):
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )

        for i in range(5):
            create_group(
                team_id=self.team.pk,
                group_type_index=0,
                group_key=f"org:{i}",
                properties={"industry": f"{i}"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {
                            "key": "industry",
                            "type": "group",
                            "value": [8],
                            "operator": "exact",
                            "group_type_index": 0,
                        }
                    ],
                    "rollout_percentage": 25,
                },
                "group_type_index": 0,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 0, "total_users": 5}, response_json)

    def test_user_blast_radius_with_groups_all_selected(self):
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="company", group_type_index=1
        )

        for i in range(5):
            create_group(
                team_id=self.team.pk,
                group_type_index=1,
                group_key=f"org:{i}",
                properties={"industry": f"{i}"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [],
                    "rollout_percentage": 25,
                },
                "group_type_index": 1,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 5, "total_users": 5}, response_json)

    @snapshot_clickhouse_queries
    def test_user_blast_radius_with_groups_multiple_queries(self):
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="company", group_type_index=1
        )

        for i in range(10):
            create_group(
                team_id=self.team.pk,
                group_type_index=0,
                group_key=f"org:{i}",
                properties={"industry": f"{i}"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {
                            "key": "industry",
                            "type": "group",
                            "value": [0, 1, 2, 3, 4],
                            "operator": "exact",
                            "group_type_index": 0,
                        },
                        {
                            "key": "industry",
                            "type": "group",
                            "value": [2, 3, 4, 5, 6],
                            "operator": "exact",
                            "group_type_index": 0,
                        },
                    ],
                    "rollout_percentage": 25,
                },
                "group_type_index": 0,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response_json = response.json()
        self.assertDictContainsSubset({"users_affected": 3, "total_users": 10}, response_json)

    def test_user_blast_radius_with_groups_incorrect_group_type(self):
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )
        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="company", group_type_index=1
        )

        for i in range(10):
            create_group(
                team_id=self.team.pk,
                group_type_index=0,
                group_key=f"org:{i}",
                properties={"industry": f"{i}"},
            )

        response = self.client.post(
            f"/api/projects/{self.team.id}/feature_flags/user_blast_radius",
            {
                "condition": {
                    "properties": [
                        {
                            "key": "industry",
                            "type": "group",
                            "value": [0, 1, 2, 3, 4],
                            "operator": "exact",
                            "group_type_index": 0,
                        },
                        {
                            "key": "industry",
                            "type": "group",
                            "value": [2, 3, 4, 5, 6],
                            "operator": "exact",
                            "group_type_index": 0,
                        },
                    ],
                    "rollout_percentage": 25,
                },
                "group_type_index": 1,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response_json = response.json()
        self.assertDictContainsSubset(
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "Invalid group type index for feature flag condition.",
            },
            response_json,
        )


class QueryTimeoutWrapper:
    def __call__(self, execute, *args, **kwargs):
        # execute so we capture queries in snapshots
        execute(*args, **kwargs)
        raise OperationalError("canceling statement due to statement timeout")


def slow_query(execute, sql, *args, **kwargs):
    if "statement_timeout" in sql:
        return execute(sql, *args, **kwargs)
    return execute(f"SELECT pg_sleep(1); {sql}", *args, **kwargs)


class TestResiliency(TransactionTestCase, QueryMatchingTest):
    def setUp(self) -> None:
        return super().setUp()

    def test_feature_flags_v3_with_group_properties(self, *args):
        self.organization = Organization.objects.create(name="test")
        self.team = Team.objects.create(organization=self.organization)
        self.user = User.objects.create_and_join(self.organization, "random@test.com", "password", "first_name")

        team_id = self.team.pk
        project_id = self.team.project_id
        rf = RequestFactory()
        create_request = rf.post(f"api/projects/{self.team.pk}/feature_flags/", {"name": "xyz"})
        create_request.user = self.user

        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )

        create_group(
            team_id=self.team.pk,
            group_type_index=0,
            group_key=f"org:1",
            properties={"industry": f"finance"},
        )

        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "group-flag",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "industry",
                                    "value": "finance",
                                    "type": "group",
                                    "group_type_index": 0,
                                }
                            ],
                            "rollout_percentage": None,
                        }
                    ],
                },
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        # Should be enabled for everyone, if groups are given
        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "default-flag",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [{"properties": [], "rollout_percentage": None}],
                },
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        with self.assertNumQueries(8):
            # one query to get group type mappings, another to get group properties
            # 2 to set statement timeout
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id", groups={"organization": "org:1"})
            self.assertTrue(all_flags["group-flag"])
            self.assertTrue(all_flags["default-flag"])
            self.assertFalse(errors)

        # now db is down
        with snapshot_postgres_queries_context(self), connection.execute_wrapper(QueryTimeoutWrapper()):
            with self.assertNumQueries(3):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team, "example_id", groups={"organization": "org:1"}
                )

                self.assertTrue("group-flag" not in all_flags)
                # can't be true unless we cache group type mappings as well
                self.assertTrue("default-flag" not in all_flags)
                self.assertTrue(errors)

            # # now db is down, but decide was sent correct group property overrides
            with self.assertNumQueries(3):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "random",
                    groups={"organization": "org:1"},
                    group_property_value_overrides={"organization": {"industry": "finance"}},
                )
                self.assertTrue("group-flag" not in all_flags)
                # can't be true unless we cache group type mappings as well
                self.assertTrue("default-flag" not in all_flags)
                self.assertTrue(errors)

            # # now db is down, but decide was sent different group property overrides
            with self.assertNumQueries(3):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "exam",
                    groups={"organization": "org:1"},
                    group_property_value_overrides={"organization": {"industry": "finna"}},
                )
                self.assertTrue("group-flag" not in all_flags)
                # can't be true unless we cache group type mappings as well
                self.assertTrue("default-flag" not in all_flags)
                self.assertTrue(errors)

    @patch("posthog.models.feature_flag.flag_matching.FLAG_EVALUATION_ERROR_COUNTER")
    def test_feature_flags_v3_with_person_properties(self, mock_counter, *args):
        self.organization = Organization.objects.create(name="test")
        self.team = Team.objects.create(organization=self.organization)
        self.user = User.objects.create_and_join(self.organization, "random@test.com", "password", "first_name")

        team_id = self.team.pk
        project_id = self.team.project_id
        rf = RequestFactory()
        create_request = rf.post(f"api/projects/{self.team.pk}/feature_flags/", {"name": "xyz"})
        create_request.user = self.user

        Person.objects.create(
            team=self.team,
            distinct_ids=["example_id"],
            properties={"email": "tim@posthog.com"},
        )

        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "property-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "value": "tim@posthog.com",
                                    "type": "person",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": None,
                        }
                    ]
                },
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        # Should be enabled for everyone
        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "default-flag",
                "filters": {"groups": [{"properties": [], "rollout_percentage": None}]},
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        with self.assertNumQueries(4):
            # 1 query to get person properties
            # 1 to set statement timeout
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id")

            self.assertTrue(all_flags["property-flag"])
            self.assertTrue(all_flags["default-flag"])
            self.assertFalse(errors)

        # now db is down
        with snapshot_postgres_queries_context(self), connection.execute_wrapper(QueryTimeoutWrapper()):
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id")

            self.assertTrue("property-flag" not in all_flags)
            self.assertTrue(all_flags["default-flag"])
            self.assertTrue(errors)

            # # now db is down, but decide was sent email parameter with correct email
            with self.assertNumQueries(0):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "random",
                    property_value_overrides={"email": "tim@posthog.com"},
                )
                self.assertTrue(all_flags["property-flag"])
                self.assertTrue(all_flags["default-flag"])
                self.assertFalse(errors)

                mock_counter.labels.assert_called_once_with(reason="timeout")
                mock_counter.labels.return_value.inc.assert_called_once_with()

            mock_counter.reset_mock()
            # # now db is down, but decide was sent email parameter with different email
            with self.assertNumQueries(0):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "example_id",
                    property_value_overrides={"email": "tom@posthog.com"},
                )
                self.assertFalse(all_flags["property-flag"])
                self.assertTrue(all_flags["default-flag"])
                self.assertFalse(errors)

                mock_counter.labels.assert_not_called()

    def test_feature_flags_v3_with_a_working_slow_db(
        self,
    ):
        self.organization = Organization.objects.create(name="test")
        self.team = Team.objects.create(organization=self.organization)
        self.user = User.objects.create_and_join(self.organization, "random@test.com", "password", "first_name")

        team_id = self.team.pk
        project_id = self.team.project_id
        rf = RequestFactory()
        create_request = rf.post(f"api/projects/{self.team.pk}/feature_flags/", {"name": "xyz"})
        create_request.user = self.user

        Person.objects.create(
            team=self.team,
            distinct_ids=["example_id"],
            properties={"email": "tim@posthog.com"},
        )

        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "property-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "value": "tim@posthog.com",
                                    "type": "person",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": None,
                        }
                    ]
                },
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        # Should be enabled for everyone
        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "default-flag",
                "filters": {"groups": [{"properties": [], "rollout_percentage": None}]},
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        with self.assertNumQueries(4):
            # 1 query to set statement timeout
            # 1 query to get person properties
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id")

            self.assertTrue(all_flags["property-flag"])
            self.assertTrue(all_flags["default-flag"])
            self.assertFalse(errors)

        # now db is slow and times out
        with (
            snapshot_postgres_queries_context(self),
            connection.execute_wrapper(slow_query),
            patch(
                "posthog.models.feature_flag.flag_matching.FLAG_MATCHING_QUERY_TIMEOUT_MS",
                500,
            ),
        ):
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id")

            self.assertTrue("property-flag" not in all_flags)
            self.assertTrue(all_flags["default-flag"])
            self.assertTrue(errors)

            # # now db is down, but decide was sent email parameter with correct email
            with self.assertNumQueries(0):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "random",
                    property_value_overrides={"email": "tim@posthog.com"},
                )
                self.assertTrue(all_flags["property-flag"])
                self.assertTrue(all_flags["default-flag"])
                self.assertFalse(errors)

            # # now db is down, but decide was sent email parameter with different email
            with self.assertNumQueries(0):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "example_id",
                    property_value_overrides={"email": "tom@posthog.com"},
                )
                self.assertFalse(all_flags["property-flag"])
                self.assertTrue(all_flags["default-flag"])
                self.assertFalse(errors)

    def test_feature_flags_v3_with_skip_database_setting(self):
        self.organization = Organization.objects.create(name="test")
        self.team = Team.objects.create(organization=self.organization)
        self.user = User.objects.create_and_join(self.organization, "random@test.com", "password", "first_name")

        team_id = self.team.pk
        project_id = self.team.project_id
        rf = RequestFactory()
        create_request = rf.post(f"api/projects/{self.team.pk}/feature_flags/", {"name": "xyz"})
        create_request.user = self.user

        Person.objects.create(
            team=self.team,
            distinct_ids=["example_id"],
            properties={"email": "tim@posthog.com"},
        )

        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "property-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "value": "tim@posthog.com",
                                    "type": "person",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": None,
                        }
                    ]
                },
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        # Should be enabled for everyone
        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "default-flag",
                "filters": {"groups": [{"properties": [], "rollout_percentage": None}]},
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        with self.assertNumQueries(0), self.settings(DECIDE_SKIP_POSTGRES_FLAGS=True):
            # No queries because of config parameter
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id")
            self.assertTrue("property-flag" not in all_flags)
            self.assertTrue(all_flags["default-flag"])
            self.assertTrue(errors)

        # db is slow and times out, but shouldn't matter to us
        with (
            self.assertNumQueries(0),
            connection.execute_wrapper(slow_query),
            patch(
                "posthog.models.feature_flag.flag_matching.FLAG_MATCHING_QUERY_TIMEOUT_MS",
                500,
            ),
            self.settings(DECIDE_SKIP_POSTGRES_FLAGS=True),
        ):
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id")

            self.assertTrue("property-flag" not in all_flags)
            self.assertTrue(all_flags["default-flag"])
            self.assertTrue(errors)

            # decide was sent email parameter with correct email
            with self.assertNumQueries(0):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "random",
                    property_value_overrides={"email": "tim@posthog.com"},
                )
                self.assertTrue(all_flags["property-flag"])
                self.assertTrue(all_flags["default-flag"])
                self.assertFalse(errors)

            # # now db is down, but decide was sent email parameter with different email
            with self.assertNumQueries(0):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "example_id",
                    property_value_overrides={"email": "tom@posthog.com"},
                )
                self.assertFalse(all_flags["property-flag"])
                self.assertTrue(all_flags["default-flag"])
                self.assertFalse(errors)

    @patch("posthog.models.feature_flag.flag_matching.FLAG_EVALUATION_ERROR_COUNTER")
    def test_feature_flags_v3_with_slow_db_doesnt_try_to_compute_conditions_again(self, mock_counter, *args):
        self.organization = Organization.objects.create(name="test")
        self.team = Team.objects.create(organization=self.organization)
        self.user = User.objects.create_and_join(self.organization, "random@test.com", "password", "first_name")

        Person.objects.create(
            team=self.team,
            distinct_ids=["example_id"],
            properties={"email": "tim@posthog.com"},
        )

        FeatureFlag.objects.create(
            name="Alpha feature",
            key="property-flag",
            filters={
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "email",
                                "value": "tim@posthog.com",
                                "type": "person",
                                "operator": "exact",
                            }
                        ],
                        "rollout_percentage": None,
                    }
                ]
            },
            team=self.team,
            created_by=self.user,
        )

        FeatureFlag.objects.create(
            name="Alpha feature",
            key="property-flag2",
            filters={
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "email",
                                "value": "tim@posthog.com",
                                "type": "person",
                                "operator": "exact",
                            }
                        ],
                        "rollout_percentage": None,
                    }
                ]
            },
            team=self.team,
            created_by=self.user,
        )

        # Should be enabled for everyone
        FeatureFlag.objects.create(
            name="Alpha feature",
            key="default-flag",
            filters={"groups": [{"properties": [], "rollout_percentage": None}]},
            team=self.team,
            created_by=self.user,
        )

        with self.assertNumQueries(4):
            # 1 query to get person properties
            # 1 query to set statement timeout
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id")

            self.assertTrue(all_flags["property-flag"])
            self.assertTrue(all_flags["default-flag"])
            self.assertFalse(errors)

        # now db is slow and times out
        with (
            snapshot_postgres_queries_context(self),
            connection.execute_wrapper(slow_query),
            patch(
                "posthog.models.feature_flag.flag_matching.FLAG_MATCHING_QUERY_TIMEOUT_MS",
                500,
            ),
            self.assertNumQueries(4),
        ):
            # no extra queries to get person properties for the second flag after first one failed
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id")

            self.assertTrue("property-flag" not in all_flags)
            self.assertTrue("property-flag2" not in all_flags)
            self.assertTrue(all_flags["default-flag"])
            self.assertTrue(errors)

            mock_counter.labels.assert_has_calls(
                [
                    call(reason="timeout"),
                    call().inc(),
                    call(reason="flag_condition_retry"),
                    call().inc(),
                ]
            )

    @patch("posthog.models.feature_flag.flag_matching.FLAG_EVALUATION_ERROR_COUNTER")
    def test_feature_flags_v3_with_group_properties_and_slow_db(self, mock_counter, *args):
        self.organization = Organization.objects.create(name="test")
        self.team = Team.objects.create(organization=self.organization)
        self.user = User.objects.create_and_join(self.organization, "randomXYZ@test.com", "password", "first_name")

        team_id = self.team.pk
        project_id = self.team.project_id
        rf = RequestFactory()
        create_request = rf.post(f"api/projects/{self.team.pk}/feature_flags/", {"name": "xyz"})
        create_request.user = self.user

        GroupTypeMapping.objects.create(
            team=self.team, project_id=self.team.project_id, group_type="organization", group_type_index=0
        )

        create_group(
            team_id=self.team.pk,
            group_type_index=0,
            group_key=f"org:1",
            properties={"industry": f"finance"},
        )

        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "group-flag",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "industry",
                                    "value": "finance",
                                    "type": "group",
                                    "group_type_index": 0,
                                }
                            ],
                            "rollout_percentage": None,
                        }
                    ],
                },
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        # Should be enabled for everyone, if groups are given
        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "default-flag",
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [{"properties": [], "rollout_percentage": None}],
                },
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        with self.assertNumQueries(8):
            # one query to get group type mappings, another to get group properties
            # 2 queries to set statement timeout
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id", groups={"organization": "org:1"})
            self.assertTrue(all_flags["group-flag"])
            self.assertTrue(all_flags["default-flag"])
            self.assertFalse(errors)

        # now db is slow
        with (
            snapshot_postgres_queries_context(self),
            connection.execute_wrapper(slow_query),
            patch(
                "posthog.models.feature_flag.flag_matching.FLAG_MATCHING_QUERY_TIMEOUT_MS",
                500,
            ),
        ):
            with self.assertNumQueries(4):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team, "example_id", groups={"organization": "org:1"}
                )

                self.assertTrue("group-flag" not in all_flags)
                # can't be true unless we cache group type mappings as well
                self.assertTrue("default-flag" not in all_flags)
                self.assertTrue(errors)

            # # now db is slow, but decide was sent correct group property overrides
            with self.assertNumQueries(4):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "random",
                    groups={"organization": "org:1"},
                    group_property_value_overrides={"organization": {"industry": "finance"}},
                )
                self.assertTrue("group-flag" not in all_flags)
                # can't be true unless we cache group type mappings as well
                self.assertTrue("default-flag" not in all_flags)
                self.assertTrue(errors)

                mock_counter.labels.assert_has_calls(
                    [
                        call(reason="timeout"),
                        call().inc(),
                        call(reason="group_mapping_retry"),
                        call().inc(),
                    ]
                )

            # # now db is down, but decide was sent different group property overrides
            with self.assertNumQueries(4):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "exam",
                    groups={"organization": "org:1"},
                    group_property_value_overrides={"organization": {"industry": "finna"}},
                )
                self.assertTrue("group-flag" not in all_flags)
                # can't be true unless we cache group type mappings as well
                self.assertTrue("default-flag" not in all_flags)
                self.assertTrue(errors)

    @patch("posthog.models.feature_flag.flag_matching.FLAG_EVALUATION_ERROR_COUNTER")
    def test_feature_flags_v3_with_experience_continuity_working_slow_db(self, mock_counter, *args):
        self.organization = Organization.objects.create(name="test")
        self.team = Team.objects.create(organization=self.organization)
        self.user = User.objects.create_and_join(self.organization, "random12@test.com", "password", "first_name")

        team_id = self.team.pk
        project_id = self.team.project_id
        rf = RequestFactory()
        create_request = rf.post(f"api/projects/{self.team.pk}/feature_flags/", {"name": "xyz"})
        create_request.user = self.user

        Person.objects.create(
            team=self.team,
            distinct_ids=["example_id", "random"],
            properties={"email": "tim@posthog.com"},
        )

        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "property-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "value": "tim@posthog.com",
                                    "type": "person",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 91,
                        }
                    ],
                },
                "ensure_experience_continuity": True,
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        # Should be enabled for everyone
        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "default-flag",
                "filters": {"groups": [{"properties": [], "rollout_percentage": None}]},
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        with snapshot_postgres_queries_context(self), self.assertNumQueries(17):
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id", hash_key_override="random")

            self.assertTrue(all_flags["property-flag"])
            self.assertTrue(all_flags["default-flag"])
            self.assertFalse(errors)

        # db is slow and times out
        with (
            snapshot_postgres_queries_context(self),
            connection.execute_wrapper(slow_query),
            patch(
                "posthog.models.feature_flag.flag_matching.FLAG_MATCHING_QUERY_TIMEOUT_MS",
                500,
            ),
        ):
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id", hash_key_override="random")

            self.assertTrue("property-flag" not in all_flags)
            self.assertTrue(all_flags["default-flag"])
            self.assertTrue(errors)

            # # now db is slow, but decide was sent email parameter with correct email
            # still need to get hash key override from db, so should time out
            with self.assertNumQueries(4):
                all_flags, _, _, errors = get_all_feature_flags(
                    self.team,
                    "random",
                    property_value_overrides={"email": "tim@posthog.com"},
                )
                self.assertTrue("property-flag" not in all_flags)
                self.assertTrue(all_flags["default-flag"])
                self.assertTrue(errors)

            mock_counter.labels.assert_has_calls(
                [
                    call(reason="timeout"),
                    call().inc(),
                ]
            )

    @patch("posthog.models.feature_flag.flag_matching.FLAG_EVALUATION_ERROR_COUNTER")
    def test_feature_flags_v3_with_experience_continuity_and_incident_mode(self, mock_counter, *args):
        self.organization = Organization.objects.create(name="test")
        self.team = Team.objects.create(organization=self.organization)
        self.user = User.objects.create_and_join(self.organization, "random12@test.com", "password", "first_name")

        team_id = self.team.pk
        project_id = self.team.project_id
        rf = RequestFactory()
        create_request = rf.post(f"api/projects/{self.team.pk}/feature_flags/", {"name": "xyz"})
        create_request.user = self.user

        Person.objects.create(
            team=self.team,
            distinct_ids=["example_id", "random"],
            properties={"email": "tim@posthog.com"},
        )

        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "property-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "value": "tim@posthog.com",
                                    "type": "person",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 91,
                        }
                    ],
                },
                "ensure_experience_continuity": True,
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        # Should be enabled for everyone
        serialized_data = FeatureFlagSerializer(
            data={
                "name": "Alpha feature",
                "key": "default-flag",
                "filters": {"groups": [{"properties": [], "rollout_percentage": None}]},
            },
            context={"team_id": team_id, "project_id": project_id, "request": create_request},
        )
        self.assertTrue(serialized_data.is_valid())
        serialized_data.save()

        with self.assertNumQueries(9), self.settings(DECIDE_SKIP_HASH_KEY_OVERRIDE_WRITES=True):
            all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id", hash_key_override="random")

            self.assertTrue(all_flags["property-flag"])
            self.assertTrue(all_flags["default-flag"])
            self.assertFalse(errors)

        # should've been false because of the override, but incident mode, so not
        all_flags, _, _, errors = get_all_feature_flags(self.team, "example_id", hash_key_override="other_id")

        self.assertTrue(all_flags["property-flag"])
        self.assertTrue(all_flags["default-flag"])
        self.assertFalse(errors)


class TestFeatureFlagStatus(APIBaseTest, ClickhouseTestMixin):
    def setUp(self):
        cache.clear()

        # delete all keys in redis
        r = redis.get_client()
        for key in r.scan_iter("*"):
            r.delete(key)
        return super().setUp()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

    def assert_expected_response(
        self, feature_flag_id: int, expected_status: FeatureFlagStatus, expected_reason: Optional[str] = None
    ):
        response = self.client.get(
            f"/api/projects/{self.team.id}/feature_flags/{feature_flag_id}/status",
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND if expected_status == FeatureFlagStatus.UNKNOWN else status.HTTP_200_OK,
        )
        response_data = response.json()
        self.assertEqual(response_data.get("status"), expected_status)
        if expected_reason is not None:
            self.assertEqual(response_data.get("reason"), expected_reason)

    def test_flag_status_reasons(self):
        FeatureFlag.objects.all().delete()

        # Request status for non-existent flag
        self.assert_expected_response(1, FeatureFlagStatus.UNKNOWN, "Flag could not be found")

        # Request status for flag that has been soft deleted
        deleted_flag = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Deleted feature flag",
            key="deleted-feature-flag",
            team=self.team,
            deleted=True,
            active=True,
        )
        self.assert_expected_response(deleted_flag.id, FeatureFlagStatus.DELETED, "Flag has been deleted")

        # Request status for flag that is disabled, but recently called
        disabled_flag = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Disabled feature flag",
            key="disabled-feature-flag",
            team=self.team,
            active=False,
        )

        self.assert_expected_response(disabled_flag.id, FeatureFlagStatus.ACTIVE)

        # Request status for flag that has super group rolled out to <100%
        fifty_percent_super_group_flag = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="50 percent super group flag",
            key="50-percent-super-group-flag",
            team=self.team,
            active=True,
            filters={"super_groups": [{"rollout_percentage": 50, "properties": []}]},
        )

        self.assert_expected_response(fifty_percent_super_group_flag.id, FeatureFlagStatus.ACTIVE)

        # Request status for flag that has super group rolled out to 100% and specific properties
        fully_rolled_out_super_group_flag_with_properties = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="100 percent super group with properties flag",
            key="100-percent-super-group-with-properties-flag",
            team=self.team,
            active=True,
            filters={
                "super_groups": [
                    {
                        "properties": [
                            {
                                "key": "$feature_enrollment/cool-new-feature",
                                "type": "person",
                                "value": ["true"],
                                "operator": "exact",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ]
            },
        )

        self.assert_expected_response(fully_rolled_out_super_group_flag_with_properties.id, FeatureFlagStatus.ACTIVE)

        # Request status for flag that has holdout group rolled out to <100%
        fifty_percent_holdout_group_flag = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="50 percent holdout group flag",
            key="50-percent-holdout-group-flag",
            team=self.team,
            active=True,
            filters={"holdout_groups": [{"rollout_percentage": 50, "properties": []}]},
        )

        self.assert_expected_response(fifty_percent_holdout_group_flag.id, FeatureFlagStatus.ACTIVE)

        # Request status for flag that has holdout group rolled out to 100% and specific properties
        fully_rolled_out_holdout_group_flag_with_properties = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="100 percent holdout group with properties flag",
            key="100-percent-holdout-group-with-properties-flag",
            team=self.team,
            active=True,
            filters={
                "holdout_groups": [
                    {
                        "properties": [
                            {
                                "key": "name",
                                "type": "person",
                                "value": ["Smith"],
                                "operator": "contains",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ]
            },
        )

        self.assert_expected_response(fully_rolled_out_holdout_group_flag_with_properties.id, FeatureFlagStatus.ACTIVE)

        # Request status for multivariate flag with no variants set to 100%
        multivariate_flag_no_rolled_out_variants = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Multivariate flag with no variants set to 100%",
            key="multivariate-no-rolled-out-variants-flag",
            team=self.team,
            active=True,
            filters={
                "multivariate": {
                    "variants": [
                        {"key": "var1key", "name": "test", "rollout_percentage": 50},
                        {"key": "var2key", "name": "control", "rollout_percentage": 50},
                    ],
                }
            },
        )

        self.assert_expected_response(multivariate_flag_no_rolled_out_variants.id, FeatureFlagStatus.ACTIVE)

        # Request status for multivariate flag with no variants set to 100%
        multivariate_flag_rolled_out_variant = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Multivariate flag with variant set to 100%",
            key="multivariate-rolled-out-variant-flag",
            team=self.team,
            active=True,
            filters={
                "multivariate": {
                    "variants": [
                        {"key": "test", "rollout_percentage": 100},
                        {"key": "control", "rollout_percentage": 0},
                    ],
                },
                "groups": [{"variant": None, "properties": [], "rollout_percentage": 100}],
            },
        )
        self.assert_expected_response(
            multivariate_flag_rolled_out_variant.id,
            FeatureFlagStatus.STALE,
            'This flag will always use the variant "test"',
        )

        # Request status for multivariate flag with a variant set to 100% but no release condition set to 100%
        multivariate_flag_rolled_out_variant_no_rolled_out_release = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Multivariate flag with variant set to 100%, no release condition set to 100%",
            key="multivariate-rolled-out-variant-no-release-rolled-out-flag",
            team=self.team,
            active=True,
            filters={
                "multivariate": {
                    "variants": [
                        {"key": "var1key", "name": "test", "rollout_percentage": 100},
                        {"key": "var2key", "name": "control", "rollout_percentage": 0},
                    ],
                },
                "groups": [
                    {"variant": None, "properties": [], "rollout_percentage": 20},
                    {"variant": None, "properties": [], "rollout_percentage": 30},
                ],
            },
        )

        self.assert_expected_response(
            multivariate_flag_rolled_out_variant_no_rolled_out_release.id,
            FeatureFlagStatus.ACTIVE,
        )

        # Request status for multivariate flag with a variant set to 100% but no release condition set to 100%
        multivariate_flag_rolled_out_release_condition_half_variant = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Multivariate flag with release condition set to 100%, but variants still 50%",
            key="multivariate-rolled-out-release-half-variant-flag",
            team=self.team,
            active=True,
            filters={
                "multivariate": {
                    "variants": [
                        {"key": "var1key", "name": "test", "rollout_percentage": 50},
                        {"key": "var2key", "name": "control", "rollout_percentage": 50},
                    ],
                },
                "groups": [
                    {"variant": None, "properties": [], "rollout_percentage": 100},
                ],
            },
        )

        self.assert_expected_response(
            multivariate_flag_rolled_out_release_condition_half_variant.id,
            FeatureFlagStatus.ACTIVE,
        )

        # Request status for multivariate flag with variants set to 100% and a filtered release condition
        multivariate_flag_rolled_out_variant_rolled_out_filtered_release = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Multivariate flag with variant and release condition set to 100%",
            key="multivariate-rolled-out-variant-and-release-condition-with-properties-flag",
            team=self.team,
            active=True,
            filters={
                "multivariate": {
                    "variants": [
                        {"key": "var1key", "name": "test", "rollout_percentage": 100},
                        {"key": "var2key", "name": "control", "rollout_percentage": 0},
                    ],
                },
                "groups": [
                    {
                        "variant": None,
                        "properties": [
                            {
                                "key": "name",
                                "type": "person",
                                "value": ["Smith"],
                                "operator": "contains",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ],
            },
        )

        self.assert_expected_response(
            multivariate_flag_rolled_out_variant_rolled_out_filtered_release.id,
            FeatureFlagStatus.ACTIVE,
        )

        # Request status for multivariate flag with no variants set to 100%, but a filtered and fully rolled out release condition has variant override
        multivariate_flag_filtered_rolled_out_release_with_override = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Multivariate flag with release condition set to 100% and override",
            key="multivariate-rolled-out-filtered-release-condition-and-override-flag",
            team=self.team,
            active=True,
            filters={
                "multivariate": {
                    "variants": [
                        {"key": "var1key", "name": "test", "rollout_percentage": 60},
                        {"key": "var2key", "name": "control", "rollout_percentage": 40},
                    ],
                },
                "groups": [
                    {
                        "variant": "var1key",
                        "properties": [
                            {
                                "key": "name",
                                "type": "person",
                                "value": ["Smith"],
                                "operator": "contains",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ],
            },
        )

        self.assert_expected_response(
            multivariate_flag_filtered_rolled_out_release_with_override.id,
            FeatureFlagStatus.ACTIVE,
        )

        # Request status for multivariate flag with no variants set to 100%, but fully rolled out release condition has variant override
        multivariate_flag_rolled_out_release_with_override = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Multivariate flag with release condition set to 100% and override",
            key="multivariate-rolled-out-release-condition-and-override-flag",
            team=self.team,
            active=True,
            filters={
                "multivariate": {
                    "variants": [
                        {"key": "test", "rollout_percentage": 60},
                        {"key": "control", "rollout_percentage": 40},
                    ],
                },
                "groups": [
                    {
                        "variant": "test",
                        "properties": [],
                        "rollout_percentage": 100,
                    }
                ],
            },
        )
        self.assert_expected_response(
            multivariate_flag_rolled_out_release_with_override.id,
            FeatureFlagStatus.STALE,
            'This flag will always use the variant "test"',
        )

        # Request status for boolean flag with empty filters
        boolean_flag_empty_filters = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Boolean flag with empty filters",
            key="boolean-empty-filters-flag",
            team=self.team,
            active=True,
            filters={},
        )
        self.assert_expected_response(
            boolean_flag_empty_filters.id,
            FeatureFlagStatus.STALE,
            'This boolean flag will always evaluate to "true"',
        )

        # Request status for boolean flag with no fully rolled out release conditions
        boolean_flag_no_rolled_out_release_conditions = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Boolean flag with no release condition set to 100%",
            key="boolean-no-rolled-out-release-conditions-flag",
            team=self.team,
            active=True,
            filters={
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 99,
                    },
                    {
                        "properties": [],
                        "rollout_percentage": 99,
                    },
                    {
                        "properties": [
                            {
                                "key": "name",
                                "type": "person",
                                "value": ["Smith"],
                                "operator": "contains",
                            }
                        ],
                        "rollout_percentage": 100,
                    },
                ],
            },
        )

        self.assert_expected_response(
            boolean_flag_no_rolled_out_release_conditions.id,
            FeatureFlagStatus.ACTIVE,
        )

        # Request status for boolean flag with a fully rolled out release condition
        boolean_flag_rolled_out_release_condition = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Boolean flag with a release condition set to 100%",
            key="boolean-rolled-out-release-condition-flag",
            team=self.team,
            active=True,
            filters={
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "name",
                                "type": "person",
                                "value": ["Smith"],
                                "operator": "contains",
                            }
                        ],
                        "rollout_percentage": 50,
                    },
                    {
                        "properties": [],
                        "rollout_percentage": 100,
                    },
                ],
            },
        )
        self.assert_expected_response(
            boolean_flag_rolled_out_release_condition.id,
            FeatureFlagStatus.STALE,
            'This boolean flag will always evaluate to "true"',
        )

        # Request status for boolean flag with a fully rolled out release condition
        boolean_flag_rolled_out_release_condition_created_twenty_nine_days_ago = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=29),
            name="Boolean flag with a release condition set to 100%, created 29 days ago",
            key="boolean-rolled-out-release-condition-29-days-ago-flag",
            team=self.team,
            active=True,
            filters={
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 100,
                    },
                ],
            },
        )
        self.assert_expected_response(
            boolean_flag_rolled_out_release_condition_created_twenty_nine_days_ago.id,
            FeatureFlagStatus.ACTIVE,
        )

        # Request status for a boolean flag with no rolled out release conditions and has
        # been called recently
        boolean_flag_no_rolled_out_release_condition_recently_evaluated = FeatureFlag.objects.create(
            created_at=datetime.now() - timedelta(days=31),
            name="Boolean flag with a release condition set to 100%",
            key="boolean-recently-evaluated-flag",
            team=self.team,
            active=True,
            filters={
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "name",
                                "type": "person",
                                "value": ["Smith"],
                                "operator": "contains",
                            }
                        ],
                        "rollout_percentage": 50,
                    },
                ],
            },
        )

        self.assert_expected_response(
            boolean_flag_no_rolled_out_release_condition_recently_evaluated.id, FeatureFlagStatus.ACTIVE
        )
