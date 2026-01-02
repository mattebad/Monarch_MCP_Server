#!/usr/bin/env python3
"""
Standalone script to perform interactive Monarch Money login with MFA support.
Run this script once to authenticate and store a token securely in your OS keyring
(Windows Credential Manager / macOS Keychain / etc.).

Supports:
- Email/password + MFA (direct API login via `monarchmoney`)
- SSO accounts (Apple/Google): paste a token copied from an authenticated browser session
"""

import asyncio
import getpass
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Literal

# Add the src directory to the Python path for imports
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

from monarchmoney import MonarchMoney, RequireMFAException
from dotenv import load_dotenv
from monarch_mcp_server.secure_session import secure_session

AuthMethod = Literal["password", "token"]


def _choose_auth_method() -> AuthMethod:
    print("\nLogin methods:")
    print("  1) Email + password (Monarch account password)")
    print("  2) Apple/Google SSO (paste Monarch token from your browser session)")
    choice = input("Choose (1/2) [1]: ").strip().lower()
    says_token = choice in {"2", "token", "sso", "apple", "google"}
    return "token" if says_token else "password"


def _print_sso_token_instructions() -> None:
    print("\n🍎 Apple/Google SSO token import")
    print("=" * 45)
    print(
        "Monarch's API client library can't perform Apple/Google SSO directly.\n"
        "Instead, you'll log in normally in your browser, then copy the API token\n"
        "from a network request.\n"
    )
    print("1) Open https://app.monarchmoney.com and sign in with Apple/Google")
    print("2) Open DevTools → Network tab")
    print("3) Click any request to 'graphql' / 'api.monarchmoney.com'")
    print("4) In Request Headers, find 'Authorization'")
    print("   - Copy the value after 'Bearer ' (or 'Token ')")
    print("\n💡 Tip: you can paste either the raw token OR a full header like:")
    print("   Authorization: Bearer <token>")


def _normalize_token(raw: str) -> str:
    """
    Accepts a raw token or common wrapper formats (Bearer/Token header, JSON blob).
    Returns the token string suitable for MonarchMoney(token=...).
    """
    s = raw.strip()
    if not s:
        return ""

    # If user pasted JSON, try common keys first.
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
            for key in ("token", "access_token", "accessToken"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    s = val.strip()
                    break
        except Exception:
            pass

    # If user pasted a header line or something containing Bearer/Token, extract it.
    # Match "Bearer <token>" or "Token <token>" anywhere in the string.
    bearer = re.search(r"(?i)\bbearer\s+([^\s]+)", s)
    if bearer:
        return bearer.group(1).strip().strip("\"'")

    token = re.search(r"(?i)\btoken\s+([^\s]+)", s)
    if token and "authorization" in s.lower():
        return token.group(1).strip().strip("\"'")

    # Otherwise, allow a plain token or a simple prefix.
    lowered = s.lower()
    for prefix in ("bearer ", "token "):
        if lowered.startswith(prefix):
            s = s[len(prefix) :].strip()
            break

    return s.strip().strip("\"'")


async def _test_connection(mm: MonarchMoney) -> int:
    print("Calling get_accounts()...")
    accounts = await mm.get_accounts()
    if not accounts or not isinstance(accounts, dict):
        raise RuntimeError(f"Unexpected response from get_accounts(): {accounts!r}")
    return len(accounts.get("accounts", []))


async def main():
    load_dotenv()
    
    print("\n🏦 Monarch Money - MCP Setup (Cursor / Claude / etc.)")
    print("=" * 45)
    print("This will authenticate you once and store a token securely")
    print("for seamless access through your MCP client.\n")
    
    # Check the version first
    try:
        import monarchmoney
        print(f"📦 MonarchMoney version: {getattr(monarchmoney, '__version__', 'unknown')}")
    except Exception as e:
        print(f"⚠️  Could not check version: {e}")
    
    try:
        # Clear any existing sessions (both old pickle files and keyring)
        secure_session.delete_token()
        print("🗑️ Cleared existing secure sessions")
        
        # Ask about MFA setup
        print("\n🔐 Security Check:")
        has_mfa = input("Do you have MFA (Multi-Factor Authentication) enabled on your Monarch Money account? (y/n): ").strip().lower()
        
        if has_mfa not in ['y', 'yes']:
            print("\n⚠️  SECURITY RECOMMENDATION:")
            print("=" * 50)
            print("You should enable MFA for your Monarch Money account.")
            print("MFA adds an extra layer of security to protect your financial data.")
            print("\nTo enable MFA:")
            print("1. Log into Monarch Money at https://monarchmoney.com")
            print("2. Go to Settings → Security")
            print("3. Enable Two-Factor Authentication")
            print("4. Follow the setup instructions\n")
            
            proceed = input("Continue with login anyway? (y/n): ").strip().lower()
            if proceed not in ['y', 'yes']:
                print("Login cancelled. Please set up MFA and try again.")
                return
        
        auth_method = _choose_auth_method()

        print("\nStarting login...")
        if auth_method == "password":
            email = input("Email: ")
            password = getpass.getpass("Password: ")
            mm = MonarchMoney()

            # Try login without MFA first
            try:
                await mm.login(
                    email, password, use_saved_session=False, save_session=True
                )
                print("✅ Login successful!")

            except RequireMFAException:
                print("🔐 MFA code required")
                mfa_code = input("Two Factor Code: ")

                # Use the same instance for MFA
                await mm.multi_factor_authenticate(email, password, mfa_code)
                print("✅ MFA authentication successful")
                mm.save_session()  # Manually save the session

        else:
            _print_sso_token_instructions()
            raw_token = getpass.getpass("\nPaste Monarch token (input hidden): ")
            token = _normalize_token(raw_token)
            if not token:
                print("❌ No token provided. Please re-run and paste a token.")
                return
            mm = MonarchMoney(token=token)
        
        # Test the connection first
        print("\nTesting connection...")
        try:
            account_count = await _test_connection(mm)
            print(f"✅ Found {account_count} accounts")
        except Exception as test_error:
            print(f"❌ Connection test failed: {test_error}")
            print(f"Error type: {type(test_error)}")

            if auth_method == "token":
                print("\n💡 Token login failed. Common causes:")
                print("- You copied the wrong value (copy the part AFTER 'Bearer ')")
                print("- The token expired; refresh the page and copy a new one")
                print("- You're on a different Monarch workspace/account than expected")
                return

            # Password login troubleshooting: try clearing any local .mm session cache and retry
            if os.path.exists(".mm"):
                print("\nAttempting cleanup of local session files and retry...")
                try:
                    shutil.rmtree(".mm")
                    print("🗑️ Cleared local .mm session files")
                except Exception:
                    pass

            print("\nPlease re-run login_setup.py and try again.")
            return
        
        # Save session securely to keyring
        try:
            print(f"\n🔐 Saving session securely to system keyring...")
            secure_session.save_authenticated_session(mm)
            print(f"✅ Session saved securely to keyring!")
                
        except Exception as save_error:
            print(f"❌ Could not save session to keyring: {save_error}")
            print("You may need to run the login again.")
        
        print("\n🎉 Setup complete! You can now use these tools in Cursor (or any MCP client):")
        print("   • get_accounts - View all your accounts")  
        print("   • get_transactions - Recent transactions")
        print("   • get_budgets - Budget information")
        print("   • get_cashflow - Income/expense analysis")
        print("\n💡 Token will persist across restarts (stored in your OS keyring).")
        
    except Exception as e:
        print(f"\n❌ Login failed: {e}")
        print("\nPlease check your credentials and try again.")
        print(f"Error type: {type(e)}")

if __name__ == "__main__":
    asyncio.run(main())