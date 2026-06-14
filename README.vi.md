# 🚀 Kiro 9router Manager

**🌐 Ngôn ngữ / Language:** [English](./README.md) · **Tiếng Việt**

> **Công cụ desktop GUI giúp bạn quản lý, đăng nhập và nạp tài khoản Kiro (AWS CodeWhisperer / Kiro IDE) vào 9router một cách tự động — nhanh, gọn, hàng loạt.**

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![PyInstaller](https://img.shields.io/badge/Build-PyInstaller-orange?logo=pyinstaller&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)
![GUI](https://img.shields.io/badge/UI-Tkinter%20Dark%20Theme-9cf)

---

## 📖 Giới thiệu

**Kiro 9router Manager** là một ứng dụng desktop viết bằng **Python + Tkinter** (giao diện *dark theme* dễ nhìn) giúp bạn **quản lý nhiều tài khoản Kiro** và tự động hóa quá trình đăng nhập chúng vào **9router** (một *local AI model router/proxy*) cũng như vào **IDE Kiro**.

Thay vì đăng nhập thủ công từng tài khoản một, công cụ cho phép bạn:

- Lưu trữ và theo dõi trạng thái của nhiều tài khoản trong một bảng trực quan.
- Đăng nhập / đăng nhập lại **hàng loạt** chỉ với vài cú click.
- Nạp token vào **cả 9router lẫn IDE Kiro** cùng lúc.
- Đăng nhập an toàn qua **device-flow OIDC** (AWS Builder ID / IAM Identity Center) mà **không cần mật khẩu, không MFA, không automation trình duyệt**.

Tất cả gói gọn trong một GUI 5 tab gọn gàng, hoặc dùng dưới dạng **CLI** cho ai thích tự động hóa.

---

## ✨ Tính năng nổi bật

- 🗂️ **Quản lý đa tài khoản** — bảng `Treeview` hiển thị toàn bộ tài khoản kèm trạng thái `OK` / `Lỗi` / `Chưa từng đăng nhập`.
- ⚡ **Đăng nhập hàng loạt** — chọn nhiều dòng, đăng nhập / relogin một loạt, mở thẳng IDE Kiro.
- 🔐 **Device-flow OIDC (tính năng mạnh nhất)** — đăng nhập qua **AWS Builder ID** hoặc **IAM Identity Center** mà không cần password/MFA/browser automation. Tool hiện mã + link → bạn bấm *Allow* → tool tự lấy `accessToken` + `refreshToken` **thật** (refresh được lâu dài).
- 📥 **Nhập linh hoạt** — thêm từng tài khoản hoặc dán hàng loạt theo định dạng `mail|pass|startUrl|mfaSecret`.
- 🧩 **Parse JSON token đa định dạng** — nhận file `kiro-auth-token.json`, mảng nhiều account, hoặc export từ 9router → tự nhận diện và nạp vào.
- 🔄 **Tự refresh token** — hỗ trợ cả OIDC (`oidc.{region}.amazonaws.com/token`) lẫn social auth (`prod.us-east-1.auth.desktop.kiro.dev/refreshToken`).
- 💾 **Account store thread-safe** — lưu JSON với *atomic write*, giữ cả `mfaSecret` + `password` để relogin tự động.
- 🪪 **Hỗ trợ cả Social Auth lẫn IAM Identity Center (IDC)**.
- 🧠 **Tích hợp AWS SSO cache** — ghi token vào `~/.aws/sso/cache` để mở IDE Kiro là đã đăng nhập sẵn.
- 📲 **Báo cáo qua Telegram** (tùy chọn) — gửi kết quả đăng nhập qua biến môi trường `TELEGRAM_CHAT_ID` + `HERMES_EXE`.
- 📦 **Đóng gói `.exe`** — build sẵn bằng **PyInstaller** với file `Kiro9RouterImporter.spec`.

---

## 🖼️ Ảnh / Demo

Giao diện **dark theme** gồm 5 tab gọn gàng:

### 1️⃣ Tab Tài khoản — bảng quản lý đa tài khoản
![Tab Tài khoản](docs/screenshots/01-accounts.jpg)

### 2️⃣ Tab Thêm / Nhập — thêm thủ công hoặc dán hàng loạt
![Tab Thêm / Nhập](docs/screenshots/02-add-import.jpg)

### 3️⃣ Tab Đăng nhập JSON — nạp token JSON đa định dạng
![Tab Đăng nhập JSON](docs/screenshots/03-json-login.jpg)

### 4️⃣ Tab Builder ID / SSO — device-flow OIDC (không cần password/MFA)
![Tab Builder ID / SSO](docs/screenshots/04-builderid-sso.jpg)

### 5️⃣ Tab Cài đặt — cấu hình 9router, DB, Chrome, Telegram
![Tab Cài đặt](docs/screenshots/05-settings.jpg)

---

## 🧰 Yêu cầu hệ thống

| Thành phần | Yêu cầu |
|---|---|
| **Python** | 3.10 trở lên |
| **Node.js** | Cần thiết cho engine `.mjs` (dùng `playwright-core`) |
| **9router** | Đang chạy local (mặc định `http://127.0.0.1:20128`) |
| **Google Chrome** | Dùng cho luồng automation trình duyệt |
| **Hệ điều hành** | Windows (khuyến nghị) |

---

## ⚙️ Cài đặt & Chạy

### 1. Clone repo

```bash
git clone https://github.com/Thangterter-Pipo/kiro-9router-manager.git
cd kiro-9router-manager
```

### 2. Cài đặt phụ thuộc

```bash
# Phần lõi GUI dùng thư viện chuẩn Python — không cần pip install gì thêm.
npm install playwright-core   # cho engine .mjs (browser automation)
```

### 3. Khởi động 9router

Đảm bảo 9router đang chạy ở địa chỉ local (mặc định `http://127.0.0.1:20128`).

### 4. Chạy GUI

```bash
python scripts/kiro_9router_gui.py
```

### 🔧 Dùng qua CLI

```bash
# Chạy lõi / import từ file
python scripts/kiro_9router_app.py --input <file> --timeout-minutes N

# Đăng nhập device-flow OIDC (Builder ID / IdC)
python scripts/kiro_device_login.py \
    --start-url https://view.awsapps.com/start \
    --targets 9router,ide

# Đăng nhập từ token JSON
python scripts/kiro_json_login.py --file token.json --targets 9router,ide
```

---

## 🧭 Hướng dẫn dùng từng Tab

### 1️⃣ Tab **Tài khoản**
Trung tâm quản lý của bạn. Bảng `Treeview` liệt kê toàn bộ tài khoản kèm trạng thái. Tại đây bạn có thể:
- Chọn **nhiều dòng** để **đăng nhập / đăng nhập lại hàng loạt**.
- Bấm **Vào IDE Kiro** để mở IDE với tài khoản đã chọn.
- **Sửa** / **Xóa** tài khoản.
- Theo dõi trạng thái `OK` / `Lỗi` / `Chưa từng đăng nhập`.

### 2️⃣ Tab **Thêm / Nhập**
Thêm tài khoản vào store:
- Thêm **một** tài khoản qua form.
- Hoặc **dán hàng loạt** theo định dạng:
  ```
  mail|pass|startUrl|mfaSecret
  ```

### 3️⃣ Tab **Đăng nhập JSON**
Dán token Kiro dạng **JSON** — hỗ trợ nhiều định dạng:
- File `kiro-auth-token.json`
- Mảng nhiều account
- Export từ 9router

Tool tự **parse** và nạp vào 9router / IDE. Có **tùy chọn refresh token**.

### 4️⃣ Tab **Builder ID / SSO** 🏆 *(mạnh nhất)*
Đăng nhập qua **device-flow OIDC** — không cần mật khẩu, MFA hay automation trình duyệt:
1. Bấm nút đăng nhập.
2. Tool hiển thị **mã** + **link**.
3. Bạn mở link, bấm **Allow**.
4. Tool tự lấy `accessToken` + `refreshToken` **thật** (refresh lâu dài).

Hỗ trợ cả **AWS Builder ID** và **IAM Identity Center**.

### 5️⃣ Tab **Cài đặt**
Cấu hình:
- Địa chỉ **9router**.
- **Đường dẫn DB**.

---

## 📦 Build `.exe`

Đóng gói ứng dụng thành file thực thi độc lập bằng PyInstaller:

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm Kiro9RouterImporter.spec
```

File `Kiro9RouterImporter.spec` đã có sẵn trong repo, cấu hình mọi thứ cần thiết.

---

## 🌱 Biến môi trường (tùy chọn)

| Biến | Mô tả |
|---|---|
| `NINEROUTER_DB` | Đường dẫn tới DB của 9router |
| `NINEROUTER_BASE_URL` | Địa chỉ base URL của 9router |
| `CHROME_PATH` | Đường dẫn tới Google Chrome |
| `TELEGRAM_CHAT_ID` | Chat ID để gửi báo cáo kết quả qua Telegram |
| `HERMES_EXE` | Đường dẫn executable dùng cho báo cáo Telegram |

---

## 🗂️ Cấu trúc dự án

```
scripts/
├── kiro_9router_gui.py                        # GUI chính (5 tab, dark theme)
├── kiro_9router_app.py                        # Entry point lõi / CLI
├── kiro_account_store.py                      # Quản lý lưu trữ tài khoản (JSON, thread-safe)
├── kiro_device_login.py                       # Đăng nhập device-flow OIDC (Builder ID / IdC)
├── kiro_json_login.py                         # Đăng nhập từ token JSON
├── kiro_ide_login.py                          # Ghi token vào AWS SSO cache cho IDE Kiro
├── ninerouter_kiro_login.py                   # Backend login + ghi 9router DB
├── ninerouter_kiro_bulk_import.py             # Import hàng loạt
├── ninerouter_kiro_idc_auto_import.mjs        # Engine browser automation (Node + playwright-core)
└── ninerouter_kiro_idc_interactive_import.py  # Import tương tác

Kiro9RouterImporter.spec                       # Cấu hình build PyInstaller
```

---

## ⚠️ Disclaimer & License

> **Lưu ý quan trọng:** Công cụ này **CHỈ** dùng cho các tài khoản Kiro **hợp lệ mà chính bạn sở hữu**. Chúng tôi **không khuyến khích** bất kỳ hành vi nào vi phạm hoặc lạm dụng điều khoản dịch vụ của AWS / Kiro. Người dùng tự chịu trách nhiệm về việc sử dụng của mình.

Dự án được phát hành theo giấy phép **MIT** — tự do sử dụng, chỉnh sửa và phân phối.

---

## 👤 Tác giả

**Thangterter-Pipo**
🔗 [github.com/Thangterter-Pipo](https://github.com/Thangterter-Pipo)

---

<p align="center">⭐ Nếu thấy hữu ích, hãy để lại một star cho repo nhé! ⭐</p>
