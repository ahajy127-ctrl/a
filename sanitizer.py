#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔐 Log Sanitizer
================
حفاظت از داده‌های حساس در لاگ‌ها
- مخفی کردن کلیدهای خصوصی
- مخفی کردن توکن‌های API
- مخفی کردن آدرس‌های کیف پول
"""

import re
import os

class LogSanitizer:
    """مخفی کردن اطلاعات حساس از لاگ‌ها"""
    
    @staticmethod
    def sanitize(text):
        """مخفی کردن داده‌های حساس از متن"""
        if not text:
            return text
        
        result = text
        
        # مخفی کردن کلیدهای خصوصی (۶۶ کاراکتر، ۶۴ hex + 0x)
        result = re.sub(
            r'0x[a-fA-F0-9]{64}',
            '0x[PRIVATE_KEY_MASKED]',
            result,
            flags=re.IGNORECASE
        )
        
        # مخفی کردن آدرس‌های کیف پول (۴۲ کاراکتر)
        result = re.sub(
            r'0x[a-fA-F0-9]{40}',
            '0x[ADDRESS_MASKED]',
            result,
            flags=re.IGNORECASE
        )
        
        # مخفی کردن توکن‌های تلگرام
        result = re.sub(
            r'\d{9,10}:[A-Za-z0-9_-]{35,}',
            '[TELEGRAM_TOKEN_MASKED]',
            result
        )
        
        # مخفی کردن Chat IDs
        result = re.sub(
            r'(?:chat_id|TG_CHAT)["\s:=]*(\d{8,})',
            r'[CHAT_ID_MASKED]',
            result,
            flags=re.IGNORECASE
        )
        
        return result
    
    @staticmethod
    def sanitize_dict(data):
        """مخفی کردن فیلدهای حساس در دیکشنری"""
        if not isinstance(data, dict):
            return data
        
        sensitive_keys = {
            'HL_AGENT_PRIVATE_KEY',
            'HL_ACCOUNT_ADDRESS',
            'TG_TOKEN',
            'TG_CHAT',
            'DASH_PASS',
            'private_key',
            'api_key',
            'token',
            'password',
        }
        
        result = {}
        for k, v in data.items():
            if k.upper() in sensitive_keys:
                result[k] = '[MASKED]'
            elif isinstance(v, str):
                result[k] = LogSanitizer.sanitize(v)
            else:
                result[k] = v
        
        return result
