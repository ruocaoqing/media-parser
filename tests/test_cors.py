import os
import unittest
from unittest.mock import patch
from app import create_app


class CorsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True})
        self.client = self.app.test_client()

    def test_api_cors_headers_present(self):
        """测试 /api/* 接口返回跨域头"""
        origin = 'http://localhost:5173'
        response = self.client.get('/api/health', headers={'Origin': origin})
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.headers.get('Access-Control-Allow-Origin'), ['*', origin])

    def test_api_options_preflight(self):
        """测试 /api/* 接口的 OPTIONS 预检请求"""
        origin = 'http://localhost:3000'
        response = self.client.options(
            '/api/parse',
            headers={
                'Origin': origin,
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'Content-Type, Authorization',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.headers.get('Access-Control-Allow-Origin'), ['*', origin])
        self.assertIn('POST', response.headers.get('Access-Control-Allow-Methods', ''))

    def test_non_api_route_cors_not_allowed(self):
        """测试非 /api/* 页面（如首页）不开放全站跨域头"""
        origin = 'http://evil.com'
        response = self.client.get('/', headers={'Origin': origin})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))

    def test_custom_cors_origins(self):
        """测试通过环境变量自定义 CORS_ORIGINS 白名单"""
        with patch.dict(os.environ, {'CORS_ORIGINS': 'https://custom.example.com, https://ui.example.com'}):
            custom_app = create_app({'TESTING': True})
            custom_client = custom_app.test_client()

            # 允许列表内的来源
            res1 = custom_client.get('/api/health', headers={'Origin': 'https://custom.example.com'})
            self.assertEqual(res1.headers.get('Access-Control-Allow-Origin'), 'https://custom.example.com')

            res2 = custom_client.get('/api/health', headers={'Origin': 'https://ui.example.com'})
            self.assertEqual(res2.headers.get('Access-Control-Allow-Origin'), 'https://ui.example.com')

            # 不在允许列表内的来源
            res3 = custom_client.get('/api/health', headers={'Origin': 'https://other.example.com'})
            self.assertIsNone(res3.headers.get('Access-Control-Allow-Origin'))


if __name__ == '__main__':
    unittest.main()
