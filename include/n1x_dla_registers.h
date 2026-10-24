/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef N1X_DLA_REGISTERS_H
#define N1X_DLA_REGISTERS_H
#include "n1x_dla_firmware.h"

/* Maps 1/2 correspond to Windows context field +0x478. DIRECT means no
 * translation table; it does not claim that the Windows mode-0 installer works.
 * None of these modes is autodetected for the live P16. */
enum n1x_dla_register_map {
    N1X_DLA_MAP_DIRECT = 0,
    N1X_DLA_MAP_WINDOWS_1 = 1,
    N1X_DLA_MAP_WINDOWS_2 = 2,
};
/* Translate one aligned u32 access and check its complete resource bounds.
 * Pure calculation; does not map or access the resource. Output is unchanged
 * on failure. The caller must independently establish the live map selection.
 */
int n1x_dla_register_offset(enum n1x_dla_register_map map, __u32 logical,
                            size_t resource_size, __u32 *mapped);
#endif
