"""The DocuSeal wrapper: auth header, payload shapes, response normalisation
and error surfacing — all against a mocked `requests`, no instance needed."""

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from crm import docuseal
from crm.tests import factories


def _response(status=200, json_body=None, content=b""):
    r = mock.Mock()
    r.status_code = status
    r.content = content
    r.text = str(json_body or "")
    if json_body is None:
        r.json.side_effect = ValueError("no body")
    else:
        r.json.return_value = json_body
    return r


@override_settings(DOCUSEAL_URL="http://sign.test", DOCUSEAL_API_TOKEN="tok-123")
class WrapperTests(SimpleTestCase):
    def test_is_configured_needs_both_settings(self):
        self.assertTrue(docuseal.is_configured())
        with override_settings(DOCUSEAL_API_TOKEN=""):
            self.assertFalse(docuseal.is_configured())
        with override_settings(DOCUSEAL_URL=""):
            self.assertFalse(docuseal.is_configured())

    @mock.patch("crm.docuseal.requests.request")
    def test_request_sends_token_to_api_base(self, request):
        request.return_value = _response(json_body={"data": []})
        docuseal.list_templates()
        args, kwargs = request.call_args
        self.assertEqual(args, ("GET", "http://sign.test/api/templates"))
        self.assertEqual(kwargs["headers"], {"X-Auth-Token": "tok-123"})

    @mock.patch("crm.docuseal.requests.request")
    def test_list_templates_unwraps_paginated_data(self, request):
        request.return_value = _response(json_body={"data": [{"id": 7, "name": "Agreement"}]})
        self.assertEqual(docuseal.list_templates(), [{"id": 7, "name": "Agreement"}])

    @mock.patch("crm.docuseal.requests.request")
    def test_create_submission_payload_and_dict_response(self, request):
        request.return_value = _response(json_body={"id": 55, "submitters": [{"id": 99}]})
        result = docuseal.create_submission(
            template_id=7,
            signer_email="jane@example.com",
            signer_name="Jane Doe",
            values={"Customer Name": "Jane Doe"},
            message="Please sign",
        )
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["template_id"], 7)
        self.assertTrue(payload["send_email"])
        # The signing-link placeholder is appended: a custom message replaces
        # DocuSeal's whole email body, which would otherwise drop the link.
        self.assertEqual(payload["message"], {"body": "Please sign\n\n{{submitter.link}}"})
        self.assertEqual(payload["submitters"], [{
            "email": "jane@example.com",
            "name": "Jane Doe",
            "values": {"Customer Name": "Jane Doe"},
        }])
        self.assertEqual(result, {"submission_id": 55, "submitter_id": 99})

    @mock.patch("crm.docuseal.requests.request")
    def test_create_submission_normalises_legacy_list_response(self, request):
        request.return_value = _response(json_body=[{"id": 99, "submission_id": 55}])
        result = docuseal.create_submission(template_id=7, signer_email="jane@example.com")
        self.assertEqual(result, {"submission_id": 55, "submitter_id": 99})

    @mock.patch("crm.docuseal.requests.request")
    def test_api_error_surfaces_detail(self, request):
        request.return_value = _response(status=422, json_body={"error": "Field missing"})
        with self.assertRaisesMessage(docuseal.DocuSealError, "422: Field missing"):
            docuseal.create_submission(template_id=7, signer_email="jane@example.com")

    @mock.patch("crm.docuseal.requests.request")
    def test_archive_submission_deletes(self, request):
        request.return_value = _response(json_body={})
        docuseal.archive_submission(55)
        self.assertEqual(
            request.call_args.args, ("DELETE", "http://sign.test/api/submissions/55"))

    @mock.patch("crm.docuseal.requests.get")
    def test_download_file_returns_bytes_and_raises_on_error(self, get):
        get.return_value = _response(content=b"%PDF-1.7")
        self.assertEqual(docuseal.download_file("http://sign.test/f.pdf"), b"%PDF-1.7")
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Auth-Token": "tok-123"})
        get.return_value = _response(status=404)
        with self.assertRaises(docuseal.DocuSealError):
            docuseal.download_file("http://sign.test/missing.pdf")


class PrefillValuesTests(TestCase):
    def test_prefill_draws_from_deal_and_customer(self):
        deal = factories.make_deal(name="MRI scanner refinance")
        values = docuseal.build_prefill_values(deal)
        self.assertEqual(values["Deal Name"], "MRI scanner refinance")
        self.assertEqual(values["Customer Name"], deal.customer.full_name)
        self.assertEqual(values["Customer Email"], deal.customer.email)
        self.assertEqual(values["Organisation"], deal.organisation.name)
        self.assertIn("Date", values)
        self.assertNotIn("Amount", values)  # no selected quote → no amount

    def test_prefill_handles_missing_customer(self):
        deal = factories.make_deal()
        deal.customer = None
        values = docuseal.build_prefill_values(deal)
        self.assertEqual(values["Customer Name"], "")
        self.assertEqual(values["Customer Email"], "")
