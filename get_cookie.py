"""从小爱音箱 API 获取 Cookie"""

import sys


def main():
    print("=" * 55)
    print("  Get Xiaomi Speaker Cookie")
    print("=" * 55)
    print()
    print("Steps:")
    print("  1. Open this URL in your browser:")
    print("     https://userprofile.mina.mi.com/device_profile/v2/conversation")
    print("     ?source=dialogu&hardware=L05C&timestamp=123&limit=2")
    print()
    print("  2. Login with your Xiaomi account if prompted")
    print()
    print("  3. You should see JSON data (not an error page)")
    print()
    print("  4. Press F12 -> Network -> Refresh the page")
    print("     Click the request to userprofile.mina.mi.com")
    print()
    print("  5. In Request Headers, find 'Cookie:' and copy the value")
    print("     Format: deviceId=xxx; serviceToken=xxx; userId=xxx")
    print()
    print("  6. Paste the cookie below")
    print()

    cookie = input("Cookie: ").strip()

    if not cookie or "serviceToken" not in cookie:
        print("[ERROR] Invalid cookie! Must contain serviceToken")
        sys.exit(1)

    # Update config.yaml
    config_path = "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    new_lines = []
    for line in lines:
        if line.startswith("cookie:"):
            new_lines.append(f'cookie: "{cookie}"')
        else:
            new_lines.append(line)

    with open(config_path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))

    print(f"\n[OK] Cookie saved to {config_path}")
    print("Now run: .\\one_click.ps1")


if __name__ == "__main__":
    main()
