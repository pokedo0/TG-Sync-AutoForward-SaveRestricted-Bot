"""全局速率限制器，控制转发频率，防止触发 FloodWaitError。"""
import asyncio
import random
import yaml

def _get_dynamic_rate_limit(fallback_config):
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            c = yaml.safe_load(f)
            return c.get("rate_limit", {})
    except Exception:
        return fallback_config.get("rate_limit", {})

class RateLimiter:
    def __init__(self, config: dict):
        self.fallback_config = config
        rl = _get_dynamic_rate_limit(self.fallback_config)
        self.forward_interval = rl.get("forward_interval", [2, 5])
        self.batch_pause_every = rl.get("batch_pause_every", 50)
        self.batch_pause_time = rl.get("batch_pause_time", [30, 60])
        self.flood_wait_multiplier = rl.get("flood_wait_multiplier", 2)
        self.max_flood_wait = rl.get("max_flood_wait", 300)

        self._count = 0
        self._interval_scale = 1.0  # FloodWait 后翻倍
        self._lock = asyncio.Lock()

    def _current_limits(self) -> dict:
        """读取最新限流配置（支持运行时热更新 config.yaml）。"""
        return _get_dynamic_rate_limit(self.fallback_config)

    async def wait(self):
        """每次转发前调用，自动控制速率。"""
        async with self._lock:
            rl = self._current_limits()
            forward_interval = rl.get("forward_interval", self.forward_interval)
            batch_pause_every = rl.get("batch_pause_every", self.batch_pause_every)
            batch_pause_time = rl.get("batch_pause_time", self.batch_pause_time)
            self.flood_wait_multiplier = rl.get("flood_wait_multiplier", self.flood_wait_multiplier)
            self.max_flood_wait = rl.get("max_flood_wait", self.max_flood_wait)

            self._count += 1
            # 批次休息
            if self._count % batch_pause_every == 0:
                pause = random.uniform(*batch_pause_time)
                await asyncio.sleep(pause)
            else:
                interval = random.uniform(*forward_interval) * self._interval_scale
                await asyncio.sleep(interval)

    def on_flood_wait(self):
        """遇到 FloodWait 后调用，增大间隔。"""
        self._interval_scale *= self.flood_wait_multiplier

    def reset_scale(self):
        """重置间隔倍率。"""
        self._interval_scale = 1.0
