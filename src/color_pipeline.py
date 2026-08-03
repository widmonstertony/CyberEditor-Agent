#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Technical color transforms shared by preview and Resolve handoff.
供预览与 Resolve 交接共同使用的技术色彩变换。

This module intentionally uses only the Python standard library. Resolve uses
its native color-management engine; FFmpeg review files receive a generated
3D LUT so Sony PP8 footage is never shown as untreated log video.

本模块仅使用 Python 标准库。Resolve 使用原生色彩管理；FFmpeg 预览使用运行时
生成的 3D LUT，避免把 Sony PP8 素材以未还原的 Log 灰片形式输出。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence, Tuple


Matrix = Tuple[Tuple[float, float, float], ...]


def _inverse_3x3(matrix: Matrix) -> Matrix:
    """Invert one 3x3 matrix. / 求一个 3x3 矩阵的逆。"""
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) < 1e-12:
        raise ValueError("Color matrix is singular.")
    return (
        ((e * i - f * h) / determinant, (c * h - b * i) / determinant, (b * f - c * e) / determinant),
        ((f * g - d * i) / determinant, (a * i - c * g) / determinant, (c * d - a * f) / determinant),
        ((d * h - e * g) / determinant, (b * g - a * h) / determinant, (a * e - b * d) / determinant),
    )


def _multiply(left: Matrix, right: Matrix) -> Matrix:
    """Multiply two 3x3 matrices. / 两个 3x3 矩阵相乘。"""
    return tuple(
        tuple(sum(left[row][k] * right[k][column] for k in range(3)) for column in range(3))
        for row in range(3)
    )


def _matrix_vector(matrix: Matrix, values: Sequence[float]) -> Tuple[float, float, float]:
    """Multiply a 3x3 matrix by RGB values. / 将 3x3 矩阵乘以 RGB。"""
    return tuple(sum(matrix[row][column] * values[column] for column in range(3)) for row in range(3))  # type: ignore[return-value]


def _rgb_to_xyz_matrix(
    primaries: Sequence[Tuple[float, float]], white: Tuple[float, float]
) -> Matrix:
    """Derive an RGB-to-XYZ matrix from chromaticities. / 从色度坐标推导 RGB 到 XYZ 矩阵。"""
    columns = []
    for x, y in primaries:
        columns.append((x / y, 1.0, (1.0 - x - y) / y))
    unscaled: Matrix = tuple(tuple(columns[column][row] for column in range(3)) for row in range(3))
    wx, wy = white
    white_xyz = (wx / wy, 1.0, (1.0 - wx - wy) / wy)
    scales = _matrix_vector(_inverse_3x3(unscaled), white_xyz)
    return tuple(
        tuple(unscaled[row][column] * scales[column] for column in range(3))
        for row in range(3)
    )


_D65 = (0.3127, 0.3290)
_SGAMUT3_CINE = ((0.7660, 0.2750), (0.2250, 0.8000), (0.0890, -0.0870))
_REC709 = ((0.6400, 0.3300), (0.3000, 0.6000), (0.1500, 0.0600))
_SGAMUT3_CINE_TO_REC709 = _multiply(
    _inverse_3x3(_rgb_to_xyz_matrix(_REC709, _D65)),
    _rgb_to_xyz_matrix(_SGAMUT3_CINE, _D65),
)


def decode_slog3(code_value: float) -> float:
    """Decode normalized S-Log3 into scene-linear light. / 将归一化 S-Log3 解码为场景线性光。"""
    code_10bit = max(0.0, min(1.0, float(code_value))) * 1023.0
    breakpoint = 171.2102946929
    if code_10bit >= breakpoint:
        return (10.0 ** ((code_10bit - 420.0) / 261.5)) * 0.19 - 0.01
    return (code_10bit - 95.0) * 0.01125 / (breakpoint - 95.0)


def _display_encode(linear: float) -> float:
    """Apply a compact highlight roll-off and Gamma 2.4 display encoding. / 应用高光压缩与 Gamma 2.4。"""
    value = max(0.0, float(linear))
    mapped = value / (1.0 + value)
    return max(0.0, min(1.0, mapped ** (1.0 / 2.4)))


def transform_sony_pp8(rgb: Sequence[float]) -> Tuple[float, float, float]:
    """Transform S-Gamut3.Cine/S-Log3 RGB into display Rec.709. / 将 PP8 RGB 变换为显示级 Rec.709。"""
    linear_source = tuple(decode_slog3(value) for value in rgb)
    linear_rec709 = _matrix_vector(_SGAMUT3_CINE_TO_REC709, linear_source)
    return tuple(_display_encode(value) for value in linear_rec709)  # type: ignore[return-value]


def iter_cube_rows(size: int = 33) -> Iterable[str]:
    """Yield Resolve/FFmpeg-compatible 3D-LUT rows. / 生成兼容 Resolve/FFmpeg 的 3D LUT 行。"""
    if size < 2:
        raise ValueError("LUT size must be at least 2.")
    scale = float(size - 1)
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                output = transform_sony_pp8((red / scale, green / scale, blue / scale))
                yield "{:.8f} {:.8f} {:.8f}\n".format(*output)


def ensure_sony_pp8_display_lut(path: Path, size: int = 33) -> Path:
    """
    Create a deterministic technical LUT when absent and return its path.
    在 LUT 不存在时确定性生成，并返回路径。

    Parameters / 参数:
        path: Destination ``.cube`` file. / 目标 ``.cube`` 文件。
        size: Samples per color axis. / 每个颜色轴的采样数。
    """
    destination = Path(path).expanduser().resolve()
    if destination.is_file() and destination.stat().st_size > 1024:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", encoding="ascii", newline="\n") as handle:
        handle.write('TITLE "CyberEditor Sony PP8 S-Gamut3.Cine S-Log3 to Rec.709 Gamma 2.4"\n')
        handle.write(f"LUT_3D_SIZE {size}\n")
        handle.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        handle.write("DOMAIN_MAX 1.0 1.0 1.0\n")
        handle.writelines(iter_cube_rows(size))
    temporary.replace(destination)
    return destination
