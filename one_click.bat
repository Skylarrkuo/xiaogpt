@echo off
rem 账号密码、mi_did、deepseek key 全部来自 config.yaml（该文件已被 .gitignore 排除）。
rem 登录用 ~/.mi.token，由 python login_qr.py 扫码生成，会自动续期。
call conda activate xiaogpt

set http_proxy=http://127.0.0.1:7890
set https_proxy=http://127.0.0.1:7890

python xiaogpt.py --config config.yaml

cmd /k
