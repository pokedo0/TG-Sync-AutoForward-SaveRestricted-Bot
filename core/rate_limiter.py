"""全局速率限制器，控制转发频率，防止触发 FloodWaitError。"""
import asyncio
import random
import time


class RateLimiter:
    def __init__(self, config: dict):
        rl = config.get("rate_limit", {})
        self.forward_interval = rl.get("forward_interval", [2, 5])
        self.batch_pause_every = rl.get("batch_pause_every", 50)
        self.batch_pause_time = rl.get("batch_pause_time", [30, 60])
        self.flood_wait_multiplier = rl.get("flood_wait_multiplier", 2)
        self.max_flood_wait = rl.get("max_flood_wait", 300)

        self._count = 0
        self._interval_scale = 1.0  # FloodWait 后翻倍
        self._lock = asyncio.Lock()

    async def wait(self):
        """每次转发前调用，自动控制速率。"""
        async with self._lock:
            self._count += 1
            # 批次休息
            if self._count % self.batch_pause_every == 0:
                pause = random.uniform(*self.batch_pause_time)
                await asyncio.sleep(pause)
            else:
                interval = random.uniform(*self.forward_interval) * self._interval_scale
                await asyncio.sleep(interval)

    def on_flood_wait(self):
        """遇到 FloodWait 后调用，增大间隔。"""
        self._interval_scale *= self.flood_wait_multiplier

    def reset_scale(self):
        """重置间隔倍率。"""
        self._interval_scale = 1.0
