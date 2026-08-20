#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔄 Resilient API Client
=======================
مدیریت خطاهای شبکه و retry logic برای Hyperliquid API
- Exponential backoff
- Circuit breaker
- Rate limiting
"""

import time
import logging
from functools import wraps
from typing import Callable, Any, Optional

class APIError(Exception):
    """خطای API"""
    pass

class CircuitBreakerOpen(APIError):
    """Circuit breaker باز است"""
    pass

class CircuitBreaker:
    """مدیریت Circuit Breaker برای جلوگیری از بارگذاری بیش‌ازحد"""
    
    def __init__(self, failure_threshold=5, recovery_timeout=60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = 'closed'  # closed, open, half_open
    
    def is_open(self):
        """بررسی Circuit Breaker باز است یا نه"""
        if self.state == 'open':
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = 'half_open'
                self.failures = 0
                return False
            return True
        return False
    
    def record_failure(self):
        """ثبت یک failure"""
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold:
            self.state = 'open'
    
    def record_success(self):
        """ثبت یک success"""
        self.failures = 0
        self.state = 'closed'

def retry_with_backoff(
    max_retries=3,
    base_delay=1,
    max_delay=32,
    exponential_base=2
):
    """
    Decorator برای retry‌های exponential backoff
    
    Args:
        max_retries: حداکثر تعداد تلاش
        base_delay: تاخیر شروع (ثانیه)
        max_delay: حداکثر تاخیر (ثانیه)
        exponential_base: مبنای exponential (معمولاً 2)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            delay = base_delay
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (ConnectionError, TimeoutError, APIError) as e:
                    last_exception = e
                    if attempt < max_retries:
                        logging.warning(
                            f"⚠️  تلاش #{attempt + 1}/{max_retries + 1} برای {func.__name__} ناموفق: {e}"
                        )
                        logging.info(f"   ⏳ منتظری {delay:.1f} ثانیه...")
                        time.sleep(delay)
                        delay = min(delay * exponential_base, max_delay)
                    else:
                        logging.error(
                            f"❌ {func.__name__} بعد از {max_retries + 1} تلاش ناموفق بود"
                        )
                except Exception as e:
                    # خطاهای دیگر را رو‌نمی‌کنیم
                    logging.error(f"❌ خطای خاتمه‌دهنده در {func.__name__}: {e}")
                    raise
            
            raise last_exception or APIError(f"{func.__name__} failed after {max_retries + 1} retries")
        
        return wrapper
    return decorator

def rate_limited(calls_per_second=10):
    """
    Decorator برای محدود کردن نرخ API calls
    
    Args:
        calls_per_second: حداکثر calls در ثانیه
    """
    min_interval = 1.0 / calls_per_second
    last_call = [0.0]
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            elapsed = time.time() - last_call[0]
            if elapsed < min_interval:
                sleep_time = min_interval - elapsed
                time.sleep(sleep_time)
            
            last_call[0] = time.time()
            return func(*args, **kwargs)
        
        return wrapper
    return decorator

class ResilientAPIClient:
    """کلاینت API مقاوم به خطا"""
    
    def __init__(self, base_url: str, max_retries=3):
        self.base_url = base_url
        self.max_retries = max_retries
        self.circuit_breaker = CircuitBreaker()
        self.logger = logging.getLogger(__name__)
    
    @retry_with_backoff(max_retries=3)
    @rate_limited(calls_per_second=10)
    def call(self, method: str, **kwargs) -> Any:
        """
        فراخوانی API با retry و rate limiting
        
        Args:
            method: نام method
            **kwargs: پارامترها
        
        Returns:
            پاسخ API
        
        Raises:
            CircuitBreakerOpen: Circuit breaker باز است
            APIError: خطای API
        """
        if self.circuit_breaker.is_open():
            raise CircuitBreakerOpen(
                f"Circuit breaker باز است برای {self.base_url}"
            )
        
        try:
            # این جا می‌توان از requests یا هر کلاینت دیگری استفاده کرد
            self.logger.debug(f"📡 فراخوانی {method} با {kwargs}")
            
            # شبیه‌سازی فراخوانی
            # result = self._http_call(method, **kwargs)
            
            self.circuit_breaker.record_success()
            return None  # placeholder
        
        except Exception as e:
            self.circuit_breaker.record_failure()
            self.logger.error(f"❌ خطا در {method}: {e}")
            raise APIError(f"API call failed: {e}") from e

# مثال استفاده:
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    @retry_with_backoff(max_retries=2, base_delay=0.5)
    def test_api_call():
        """تست مثال"""
        import random
        if random.random() < 0.7:
            raise ConnectionError("شبیه‌سازی خطا")
        return "موفق ✅"
    
    try:
        result = test_api_call()
        print(f"نتیجه: {result}")
    except Exception as e:
        print(f"خطا: {e}")
