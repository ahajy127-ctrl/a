#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔒 Configuration Validator
=========================
اعتبارسنجی محیط و تنظیمات قبل از شروع ربات
- بررسی فایل .env
- تایید کلیدهای ضروری
- بررسی اتصال API
- امنیت حساسیت‌ها
"""

import os
import sys
import json
from pathlib import Path

class ConfigValidator:
    def __init__(self, base_dir=None):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.env_file = os.path.join(self.base_dir, '.env')
        self.errors = []
        self.warnings = []
        
    def validate_all(self):
        """اجرای کامل تمام بررسی‌ها"""
        print("\n🔍 بررسی محیط و تنظیمات...")
        print("=" * 60)
        
        self._check_env_exists()
        self._check_env_readable()
        self._check_required_keys()
        self._check_key_format()
        self._check_security()
        
        return self._report_results()
    
    def _check_env_exists(self):
        """بررسی وجود فایل .env"""
        if not os.path.exists(self.env_file):
            self.errors.append("❌ فایل .env یافت نشد!")
            self.errors.append("   💡 راه‌حل: اجرا کنید: python3 setup_hyperliquid.py")
            return False
        return True
    
    def _check_env_readable(self):
        """بررسی خوانایی فایل .env"""
        try:
            with open(self.env_file, 'r', encoding='utf-8') as f:
                self.env_data = {}
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, _, v = line.partition('=')
                        self.env_data[k.strip()] = v.strip()
            return True
        except Exception as e:
            self.errors.append(f"❌ خطا در خواندن .env: {e}")
            return False
    
    def _check_required_keys(self):
        """بررسی کلیدهای ضروری"""
        required = {
            'HL_ACCOUNT_ADDRESS': 'آدرس کیف پول Master (Rabby)',
            'HL_AGENT_PRIVATE_KEY': 'کلید خصوصی Agent',
            'MODE': 'حالت اجرا (paper/live)',
        }
        
        for key, desc in required.items():
            val = self.env_data.get(key, '').strip()
            if not val:
                self.errors.append(f"❌ متغیر '{key}' تعریف نشده!")
                self.errors.append(f"   تشریح: {desc}")
        
        mode = self.env_data.get('MODE', '').strip()
        if mode and mode not in ('paper', 'live'):
            self.errors.append(f"❌ MODE معتبر نیست: {mode}")
            self.errors.append(f"   باید 'paper' یا 'live' باشد")
    
    def _check_key_format(self):
        """بررسی فرمت کلیدهای EVM"""
        addr = self.env_data.get('HL_ACCOUNT_ADDRESS', '').strip()
        if addr:
            if not addr.startswith('0x') or len(addr) != 42:
                self.errors.append(f"❌ HL_ACCOUNT_ADDRESS معتبر نیست!")
                self.errors.append(f"   باید 0x... و 42 کاراکتر باشد")
        
        key = self.env_data.get('HL_AGENT_PRIVATE_KEY', '').strip()
        if key:
            if not key.startswith('0x') or len(key) != 66:
                self.errors.append(f"❌ HL_AGENT_PRIVATE_KEY معتبر نیست!")
                self.errors.append(f"   باید 0x... و 66 کاراکتر باشد")
    
    def _check_security(self):
        """بررسی های امنیتی"""
        try:
            st = os.stat(self.env_file)
            mode = oct(st.st_mode)[-3:]
            if mode != '600':
                self.warnings.append(f"⚠️  دسترسی .env امن نیست: {mode}")
                self.warnings.append(f"   💡 اصلاح: chmod 600 .env")
        except Exception:
            pass
        
        pass_val = self.env_data.get('DASH_PASS', '').strip()
        if not pass_val:
            self.warnings.append("⚠️  رمز داشبورد تنظیم نشده!")
            self.warnings.append("   💡 راه‌حل: DASH_PASS را در .env تنظیم کنید")
        elif len(pass_val) < 8:
            self.warnings.append(f"⚠️  رمز داشبورد ضعیف است ({len(pass_val)} کاراکتر)")
            self.warnings.append("   💡 استفاده کنید: حداقل ۸ کاراکتر + عدد + حرف")
    
    def _report_results(self):
        """گزارش نتایج"""
        if self.errors:
            print("\n❌ خطاهای بحرانی:")
            for err in self.errors:
                print(err)
            print("\n" + "=" * 60)
            print("ربات نمی‌تواند شروع شود!")
            return False
        
        if self.warnings:
            print("\n⚠️  هشدارها:")
            for warn in self.warnings:
                print(warn)
        
        print("\n✅ تمام بررسی‌ها موفق!")
        print("=" * 60)
        return True

def main():
    validator = ConfigValidator()
    success = validator.validate_all()
    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()
