#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
💾 Bounded Cache with TTL
=========================
کش محدود‌شده برای جلوگیری از نشت حافظه (Memory Leak)
- محدودیت حجم
- انقضای خودکار (TTL)
- حذف خودکار قدیمی‌ترین عنصرها
"""

import time
from collections import OrderedDict
from typing import Any, Optional

class BoundedCache:
    """کش محدود‌شده با TTL"""
    
    def __init__(self, max_size: int = 1000, ttl: float = 3600.0):
        self.max_size = max_size
        self.ttl = ttl
        self.cache = OrderedDict()
        self.timestamps = {}
    
    def get(self, key: str) -> Optional[Any]:
        """دریافت مقدار از کش"""
        if key not in self.cache:
            return None
        
        if time.time() - self.timestamps[key] > self.ttl:
            self.delete(key)
            return None
        
        self.cache.move_to_end(key)
        return self.cache[key]
    
    def set(self, key: str, value: Any) -> None:
        """ذخیره مقدار در کش"""
        if key in self.cache:
            self.cache.move_to_end(key)
            self.cache[key] = value
            self.timestamps[key] = time.time()
        else:
            if len(self.cache) >= self.max_size:
                oldest_key = next(iter(self.cache))
                self.delete(oldest_key)
            
            self.cache[key] = value
            self.timestamps[key] = time.time()
    
    def delete(self, key: str) -> None:
        """حذف عنصر"""
        if key in self.cache:
            del self.cache[key]
            del self.timestamps[key]
    
    def cleanup_expired(self) -> int:
        """حذف تمام عنصرهای منقضی‌شده"""
        now = time.time()
        expired = [
            key for key, ts in self.timestamps.items()
            if now - ts > self.ttl
        ]
        for key in expired:
            self.delete(key)
        return len(expired)
    
    def clear(self) -> None:
        """خالی کردن کش"""
        self.cache.clear()
        self.timestamps.clear()
    
    def size(self) -> int:
        """تعداد عنصرهای کش"""
        return len(self.cache)
    
    def stats(self) -> dict:
        """آمار کش"""
        return {
            'size': len(self.cache),
            'max_size': self.max_size,
            'ttl': self.ttl,
            'utilization': len(self.cache) / self.max_size,
        }

class CandleCache(BoundedCache):
    """کش خاص برای کندل‌ها"""
    
    def __init__(self, max_size: int = 500, ttl: float = 600.0):
        super().__init__(max_size=max_size, ttl=ttl)
    
    def get_candle(self, coin: str, timeframe: str, count: int) -> Optional[list]:
        """دریافت کندل‌ها"""
        key = f"{coin}:{timeframe}:{count}"
        return self.get(key)
    
    def set_candle(self, coin: str, timeframe: str, count: int, closes: list) -> None:
        """ذخیره کندل‌ها"""
        key = f"{coin}:{timeframe}:{count}"
        self.set(key, closes)
