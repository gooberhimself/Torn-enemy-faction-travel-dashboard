"""Generate a PIN verifier without putting the PIN in shell history."""
import argparse
import getpass
import re
from werkzeug.security import generate_password_hash

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate one administrator-managed role PIN verifier.')
    parser.add_argument('role', choices=('public', 'tactician'))
    args = parser.parse_args()
    pin = getpass.getpass(f'New five-digit {args.role} PIN: ')
    if not re.fullmatch(r'[0-9]{5}', pin):
        raise SystemExit('PIN must contain exactly five ASCII digits.')
    if pin != getpass.getpass('Confirm PIN: '):
        raise SystemExit('PINs do not match.')
    print(args.role.upper() + "_PIN_HASH='" + generate_password_hash(pin, method='scrypt') + "'")
