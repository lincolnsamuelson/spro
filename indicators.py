from typing import Optional, Tuple, List


def ema(prices: List[float], period: int) -> List[float]:
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(prices[:period]) / period]
    for price in prices[period:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(prices: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Optional[Tuple[float, float, float]]:
    """Returns (macd_line, signal_line, histogram) or None."""
    if len(prices) < slow + signal_period:
        return None
    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)
    # Align lengths - fast_ema is longer than slow_ema
    offset = len(fast_ema) - len(slow_ema)
    macd_line = [f - s for f, s in zip(fast_ema[offset:], slow_ema)]
    if len(macd_line) < signal_period:
        return None
    signal_line = ema(macd_line, signal_period)
    if not signal_line:
        return None
    histogram = macd_line[-1] - signal_line[-1]
    return (macd_line[-1], signal_line[-1], histogram)


def bollinger_bands(prices: List[float], period: int = 20, num_std: float = 2.0) -> Optional[Tuple[float, float, float]]:
    """Returns (upper, middle, lower) or None."""
    if len(prices) < period:
        return None
    window = prices[-period:]
    middle = sum(window) / period
    variance = sum((p - middle) ** 2 for p in window) / period
    std = variance ** 0.5
    return (middle + num_std * std, middle, middle - num_std * std)


def ema_crossover(prices: List[float], fast_period: int = 9, slow_period: int = 21) -> Optional[str]:
    """Returns 'golden_cross', 'death_cross', or None."""
    if len(prices) < slow_period + 2:
        return None
    fast_vals = ema(prices, fast_period)
    slow_vals = ema(prices, slow_period)
    offset = len(fast_vals) - len(slow_vals)
    fast_aligned = fast_vals[offset:]
    if len(fast_aligned) < 2:
        return None
    prev_diff = fast_aligned[-2] - slow_vals[-2]
    curr_diff = fast_aligned[-1] - slow_vals[-1]
    if prev_diff <= 0 and curr_diff > 0:
        return "golden_cross"
    if prev_diff >= 0 and curr_diff < 0:
        return "death_cross"
    return None
