from __future__ import annotations
import numpy as np
import pandas as pd

def signed_volume(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """Computes signed volume using the tick rule with carry-forward of last sign."""
    n = len(close)
    if n == 0:
        return np.array([])
    diff = np.diff(close)
    diff = np.insert(diff, 0, 0.0)
    
    sign = np.zeros(n)
    last_sign = 1.0  # Default start direction
    
    for i in range(n):
        if diff[i] > 0:
            last_sign = 1.0
        elif diff[i] < 0:
            last_sign = -1.0
        sign[i] = last_sign
        
    return volume * sign

def ofi(signed_vol: np.ndarray, window: int = 60) -> tuple[float, np.ndarray, np.ndarray]:
    """Computes raw and normalized Order Flow Imbalance (OFI) over a rolling window."""
    n = len(signed_vol)
    if n == 0:
        return 0.0, np.array([]), np.array([])
        
    sv_series = pd.Series(signed_vol)
    ofi_raw = sv_series.rolling(window).sum().fillna(0.0).values
    total_vol = sv_series.abs().rolling(window).sum().fillna(0.0).values
    
    ofi_norm = np.zeros_like(ofi_raw)
    valid = total_vol > 0
    ofi_norm[valid] = ofi_raw[valid] / total_vol[valid]
    return float(ofi_norm[-1]), ofi_raw, ofi_norm

def vpin(signed_vol: np.ndarray, bucket_size: float = 50000.0, n_buckets: int = 10) -> tuple[float, np.ndarray]:
    """Computes VPIN over constant volume buckets. Handles warmup (< n_buckets -> NaN).

    VPIN is the mean |buy - sell| fraction per volume bucket:
        VPIN = sum(|buy_i - sell_i|) / sum(total_volume_i)   over the last n_buckets.

    Buckets are built from whole bars, so a bucket's actual volume can exceed
    ``bucket_size`` (e.g. a 20k-share minute bar straddles a 50k boundary).
    Normalizing by the ACTUAL volume in each bucket keeps the result in [0, 1];
    dividing by ``n_buckets * bucket_size`` as before inflated VPIN above 1 on
    high-volume (spike) bars, silently rejecting every signal through the
    toxicity gate.
    """
    n = len(signed_vol)
    if n == 0:
        return np.nan, np.array([])

    abs_vol = np.abs(signed_vol)
    buy_vol = np.where(signed_vol > 0, signed_vol, 0.0)
    sell_vol = np.where(signed_vol < 0, -signed_vol, 0.0)

    cum_vol = np.cumsum(abs_vol)
    bucket_idx = (cum_vol // bucket_size).astype(int)

    df_b = pd.DataFrame({'buy': buy_vol, 'sell': sell_vol, 'bucket': bucket_idx})
    grouped = df_b.groupby('bucket').sum()

    # Imbalance and ACTUAL volume per bucket (not the nominal bucket_size).
    bucket_imb = np.abs(grouped['buy'] - grouped['sell'])
    bucket_vol = grouped['buy'] + grouped['sell']

    # Rolling VPIN: sum of imbalances / sum of actual bucket volumes.
    roll_imb = bucket_imb.rolling(n_buckets).sum()
    roll_vol = bucket_vol.rolling(n_buckets).sum()
    rolling_vpin = roll_imb / roll_vol.replace(0.0, np.nan)

    vpin_arr = np.full(n, np.nan)
    for b_id in rolling_vpin.index:
        val = rolling_vpin.loc[b_id]
        mask = (bucket_idx == b_id + 1)
        vpin_arr[mask] = val

    vpin_series = pd.Series(vpin_arr).ffill().values
    latest_vpin = float(vpin_series[-1]) if len(vpin_series) > 0 and not np.isnan(vpin_series[-1]) else np.nan
    return latest_vpin, vpin_series

def half_spread_pct(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int = 10) -> tuple[float, np.ndarray]:
    """Estimates half-spread percent using the Corwin-Schultz estimator with tick floor and volatility fallback."""
    n = len(close)
    if n == 0:
        return 0.001, np.array([])
        
    # Check if high/low columns are present or have real range
    if high is None or low is None or len(high) == 0 or np.all(high == low) or np.all(high == close):
        # Fall back to volatility proxy (rolling standard deviation of log returns)
        log_ret = np.diff(np.log(np.maximum(close, 1e-9)))
        log_ret = np.insert(log_ret, 0, 0.0)
        lr_series = pd.Series(log_ret)
        vol = lr_series.rolling(window).std().fillna(0.001).values
        half_spread = np.maximum(vol * 0.01, 0.005 / np.maximum(close, 1e-9))
        return float(half_spread[-1]), half_spread

    # Corwin-Schultz High-Low Spread Estimator
    half_spread = np.zeros(n)
    
    # Calculate log(H_t / L_t)
    hl_ratio = np.log(np.maximum(high, 1e-9) / np.maximum(low, 1e-9))
    gamma = hl_ratio ** 2
    
    # Precompute consecutive gamma sums
    gamma_consec = gamma[:-1] + gamma[1:]
    
    # Beta: ln(H_{t,t+1} / L_{t,t+1})^2
    h_consec_max = np.maximum(high[:-1], high[1:])
    l_consec_min = np.minimum(low[:-1], low[1:])
    hl_consec_ratio = np.log(h_consec_max / np.maximum(l_consec_min, 1e-9))
    beta_consec = hl_consec_ratio ** 2
    
    c = np.sqrt(2.0) + 1.0
    
    for i in range(1, n):
        g = gamma_consec[i-1]
        b = beta_consec[i-1]
        
        alpha = c * (np.sqrt(g) - np.sqrt(b))
        
        if alpha < 0 or np.isnan(alpha):
            s = 0.0
        else:
            s = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
            
        half_spread[i] = s / 2.0
        
    # Apply floor: 1 tick / price (penny / close)
    tick_floor = 0.01 / np.maximum(close, 1e-9)
    half_spread = np.maximum(half_spread, tick_floor)
    
    # Fill index 0 with the value at index 1
    if n > 1:
        half_spread[0] = half_spread[1]
        
    latest_spread = float(half_spread[-1]) if len(half_spread) > 0 else 0.001
    return latest_spread, half_spread
