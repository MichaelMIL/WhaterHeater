# Two link-command fixes the pioarduino ESP32-C2 build needs:
#
# 1. The ESP32-C2 is RV32IMC (no atomic instruction extension). The link
#    command pioarduino generates has no -march/-mabi, so the gcc driver
#    links its *default* libstdc++ multilib, which contains amoadd.w
#    instructions -> the chip crashes with "Illegal instruction" as soon as
#    shared_ptr refcounting runs. Forcing the arch at link time selects the
#    correct rv32imc multilib.
#
# 2. CONFIG_MBEDTLS_DYNAMIC_BUFFER (set via custom_sdkconfig to fit TLS in
#    the C2's small heap) works by wrapping mbedTLS entry points, but the
#    skeleton-lib flag export loses the -Wl,--wrap options that ESP-IDF's
#    mbedtls CMakeLists adds. Without them the dynamic-buffer code is never
#    linked in and TLS keeps using full-size static buffers.
Import("env")

env.Append(LINKFLAGS=["-march=rv32imc_zicsr_zifencei", "-mabi=ilp32"])

MBEDTLS_DYNAMIC_WRAPS = [
    "mbedtls_ssl_write_client_hello",
    "mbedtls_ssl_handshake_client_step",
    "mbedtls_ssl_tls13_handshake_client_step",
    "mbedtls_ssl_handshake_server_step",
    "mbedtls_ssl_tls13_handshake_server_step",
    "mbedtls_ssl_read",
    "mbedtls_ssl_write",
    "mbedtls_ssl_session_reset",
    "mbedtls_ssl_free",
    "mbedtls_ssl_setup",
    "mbedtls_ssl_send_alert_message",
    "mbedtls_ssl_close_notify",
]
env.Append(LINKFLAGS=["-Wl,--wrap=%s" % f for f in MBEDTLS_DYNAMIC_WRAPS])
