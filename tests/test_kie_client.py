import unittest

from kie_client import (
    KIEError,
    KIEQuotaError,
    KIE_CHAT_MODELS,
    KIE_IMAGE_EDIT_MODELS,
    KIE_IMAGE_MODELS,
    KIE_TRANSCRIPTION_MODELS,
    _validate,
    image_edit_input,
    image_input,
    model_base_url,
)


class KIEContractTests(unittest.TestCase):
    def test_models_match_psychobot_configuration(self):
        self.assertEqual(KIE_CHAT_MODELS, ("gemini-3-flash", "gemini-2.5-flash"))
        self.assertIn("elevenlabs/speech-to-text", KIE_TRANSCRIPTION_MODELS)
        self.assertIn("seedream/4.5-text-to-image", KIE_IMAGE_MODELS)
        self.assertIn("seedream/4.5-edit", KIE_IMAGE_EDIT_MODELS)

    def test_chat_endpoint_is_the_psychobot_openai_compatible_shape(self):
        self.assertEqual(
            model_base_url("https://api.kie.ai", "gemini-3-flash"),
            "https://api.kie.ai/gemini-3-flash/v1",
        )

    def test_kie_error_and_quota_response_handling(self):
        with self.assertRaises(KIEQuotaError):
            _validate(200, {"code": 400, "message": "insufficient credit balance"}, "test")
        with self.assertRaises(KIEError):
            _validate(503, {"message": "maintenance"}, "test")
        self.assertEqual(_validate(200, {"code": 200, "data": 1014.79}, "test"), {"code": 200, "data": 1014.79})

    def test_image_payloads_match_supported_models(self):
        self.assertEqual(image_input("seedream/4.5-text-to-image", "tarot card")["aspect_ratio"], "3:4")
        self.assertEqual(image_input("google/imagen4-fast", "wide landscape")["aspect_ratio"], "4:3")
        edit = image_edit_input("seedream/4.5-edit", "portrait", "https://example.test/image.jpg")
        self.assertEqual(edit["image_urls"], ["https://example.test/image.jpg"])
        with self.assertRaises(KIEError):
            image_input("unknown", "test")


if __name__ == "__main__":
    unittest.main()
