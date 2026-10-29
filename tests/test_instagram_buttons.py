import unittest

from composio_instagram import InstagramTransport, TransportError


class ButtonTemplateTests(unittest.TestCase):
    def setUp(self):
        self.transport = InstagramTransport(None)
        self.calls = []

        def proxy(method, path, params=None, body=None, check=True):
            self.calls.append((method, path, body))
            return {'message_id': 'synthetic-message-id'}

        self.transport.proxy = proxy

    def valid_buttons(self):
        return [
            {'type': 'postback', 'title': 'Получить', 'payload': 'GET_MATERIAL'},
            {'type': 'web_url', 'title': 'Открыть', 'url': 'https://goszakupki.info/material'},
        ]

    def test_documented_request_shape_and_single_proxy_call(self):
        result = self.transport.send_buttons('123456', 'Выберите действие', self.valid_buttons())
        self.assertEqual(result['message_id'], 'synthetic-message-id')
        self.assertEqual(len(self.calls), 1)
        method, path, body = self.calls[0]
        self.assertEqual((method, path), ('POST', self.transport.ig_id + '/messages'))
        self.assertEqual(body, {
            'recipient': {'id': '123456'},
            'message': {'attachment': {'type': 'template', 'payload': {
                'template_type': 'button', 'text': 'Выберите действие', 'buttons': self.valid_buttons()}}}})

    def test_bad_recipient_and_button_counts_fail_before_proxy(self):
        for uid in ('abc', '', '-1'):
            with self.subTest(uid=uid), self.assertRaises(TransportError):
                self.transport.send_buttons(uid, 'Выберите', self.valid_buttons())
        for buttons in ([], self.valid_buttons() * 2):
            with self.subTest(buttons=buttons), self.assertRaises(TransportError):
                self.transport.send_buttons('123', 'Выберите', buttons)
        self.assertEqual(self.calls, [])

    def test_rejects_invalid_schema_and_unsafe_urls(self):
        invalid = [
            [{'type': 'postback', 'title': 'OK', 'payload': 'SAFE', 'extra': 1}],
            [{'type': 'postback', 'title': 'x' * 21, 'payload': 'SAFE'}],
            [{'type': 'postback', 'title': 'OK', 'payload': 'bad payload'}],
            [{'type': 'web_url', 'title': 'OK', 'url': 'http://goszakupki.info'}],
            [{'type': 'web_url', 'title': 'OK', 'url': 'https://evil.example'}],
            [{'type': 'web_url', 'title': 'OK', 'url': 'https://goszakupki.info@evil.example/'}],
            [{'type': 'web_url', 'title': 'OK', 'url': 'https://goszakupki.info:443/'}],
            [{'type': 'web_url', 'title': 'OK', 'url': 'javascript:alert(1)'}],
            [{'type': 'web_url', 'title': 'OK', 'url': 'https://instagram.com/path#fragment'}],
            [{'type': 'web_url', 'title': 'OK', 'url': 'https://instagram.com/', 'extra': True}],
            [{'type': 'unknown', 'title': 'OK'}],
        ]
        for buttons in invalid:
            with self.subTest(buttons=buttons), self.assertRaises(TransportError):
                self.transport.send_buttons('123', 'Выберите', buttons)
        self.assertEqual(self.calls, [])

    def test_text_length_and_type_are_checked(self):
        for text in ('', 'x' * 641, None):
            with self.subTest(text=repr(text)), self.assertRaises(TransportError):
                self.transport.send_buttons('123', text, self.valid_buttons())
        self.assertEqual(self.calls, [])

    def test_missing_message_id_and_transport_failure_are_single_attempt(self):
        self.transport.proxy = lambda *a, **k: self.calls.append((a, k)) or {}
        with self.assertRaises(TransportError):
            self.transport.send_buttons('123', 'Выберите', self.valid_buttons())
        self.assertEqual(len(self.calls), 1)

        self.calls.clear()

        def fail(*args, **kwargs):
            self.calls.append((args, kwargs))
            raise TransportError('synthetic transport failure')

        self.transport.proxy = fail
        with self.assertRaises(TransportError):
            self.transport.send_buttons('123', 'Выберите', self.valid_buttons())
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
