"""Embedded-controller (EC) register map for Uniwill laptops.

These addresses come from the open-source Linux drivers that document this EC:
the upstream ``uniwill-laptop`` kernel driver (drivers/platform/x86/uniwill)
and TUXEDO's ``tuxedo-drivers``.  They are the same registers the Windows
Control Center reaches through its ACPIDriver.sys (ACPI device INOU0000,
methods ECRR / ECRW).

Only registers in ``WRITABLE`` may ever be written by this program.
"""

# ---- read-only sensors -----------------------------------------------------
CPU_TEMP = 0x043E
GPU_TEMP = 0x044F
SYSTEM_ID = 0x0456            # bit7 = has dedicated GPU
MAIN_FAN_RPM_HI = 0x0464      # big-endian 16-bit
MAIN_FAN_RPM_LO = 0x0465
SECOND_FAN_RPM_HI = 0x046C
SECOND_FAN_RPM_LO = 0x046D
PROJECT_ID = 0x0740
AP_OEM = 0x0741
SUPPORT_5 = 0x0742            # bit4 fan turbo supported, bit5 fan support
PWM_1 = 0x075B                # current fan duty, 0..200
PWM_2 = 0x075C
SUPPORT_1 = 0x0765            # bit6 lightbar, bit7 fan boost
SUPPORT_2 = 0x0766            # bit0 silent mode, bit2 RGB keyboard
ROMID_START = 0x0770          # 14 bytes of EC firmware id

# ---- fan / performance -----------------------------------------------------
MANUAL_FAN_CTRL = 0x0751
FAN_LEVEL_MASK = 0x07
FAN_MODE_TURBO = 0x10
FAN_MODE_HIGH = 0x20
FAN_MODE_BOOST = 0x40         # "full fan mode" - fans at maximum
FAN_MODE_USER = 0x80
# Performance profile values written into MANUAL_FAN_CTRL by TUXEDO's driver.
# The Windows app calls these Office / Gaming / Turbo.
PROFILE_MASK = 0xA0 | 0x10
PROFILE_VALUES = {"office": 0xA0, "gaming": 0x00, "turbo": 0x10}

FAN_CTRL = 0x078E
UNIVERSAL_FAN_CTRL_BIT = 0x40  # EC supports custom fan tables
UNIVERSAL_FAN_CTRL = 0x07C5
SPLIT_TABLES_BIT = 0x80        # separate tables for fan 1 (CPU) and fan 2 (GPU)
AP_OEM_6 = 0x07C6
ENABLE_UNIVERSAL_FAN_CTRL_BIT = 0x04

FAN_TABLE_LEN = 16
CPU_TEMP_END_TABLE = 0x0F00
CPU_TEMP_START_TABLE = 0x0F10
CPU_FAN_SPEED_TABLE = 0x0F20
GPU_TEMP_END_TABLE = 0x0F30
GPU_TEMP_START_TABLE = 0x0F40
GPU_FAN_SPEED_TABLE = 0x0F50
FAN_SPEED_MAX = 200            # EC fan duty scale (0xC8 == 100 %)

# ---- keyboard backlight (single zone / white) ------------------------------
TRIGGER = 0x0767
RGB_APPLY_COLOR = 0x20
RGB_RAINBOW_EFFECT = 0x80
RGB_RED = 0x0769
RGB_GREEN = 0x076A
RGB_BLUE = 0x076B
RGB_MAX = 50                   # EC intensity scale per channel

KBD_STATUS = 0x078C
KBD_WHITE_ONLY = 0x01
KBD_POWER_OFF = 0x02
KBD_APPLY = 0x10
KBD_BRIGHTNESS_SHIFT = 5
KBD_BRIGHTNESS_MASK = 0xE0

# ---- lightbar --------------------------------------------------------------
LIGHTBAR_AC_CTRL = 0x0748
LIGHTBAR_AC_RGB = (0x0749, 0x074A, 0x074B)
LIGHTBAR_BAT_CTRL = 0x07E2
LIGHTBAR_BAT_RGB = (0x07E3, 0x07E4, 0x07E5)
LIGHTBAR_APP_EXISTS = 0x01
LIGHTBAR_S0_OFF = 0x04
LIGHTBAR_WELCOME = 0x80        # EC rainbow animation
LIGHTBAR_MAX = 36

# ---- misc ------------------------------------------------------------------
BIOS_OEM = 0x074E              # bit4 Fn-lock status (read)

#: Direct fan duty registers (0..200), honoured while "full fan mode"
#: (MANUAL_FAN_CTRL bit 6) is set.  Used for software fan curves on ECs
#: without universal fan control - only reachable through the WMI interface.
PWM_1_DIRECT = 0x1804
PWM_2_DIRECT = 0x1809

#: The ECRR/ECRW ACPI methods only accept addresses up to 0xFFF.
EC_MAX_ADDR = 0x0FFF

#: Registers this program is allowed to write.  Anything else is refused.
WRITABLE = frozenset(
    {
        MANUAL_FAN_CTRL,
        UNIVERSAL_FAN_CTRL,
        AP_OEM_6,
        TRIGGER,
        RGB_RED,
        RGB_GREEN,
        RGB_BLUE,
        KBD_STATUS,
        LIGHTBAR_AC_CTRL,
        LIGHTBAR_BAT_CTRL,
        *LIGHTBAR_AC_RGB,
        *LIGHTBAR_BAT_RGB,
        PWM_1_DIRECT,
        PWM_2_DIRECT,
    }
    | set(range(CPU_TEMP_END_TABLE, GPU_FAN_SPEED_TABLE + FAN_TABLE_LEN))
)
