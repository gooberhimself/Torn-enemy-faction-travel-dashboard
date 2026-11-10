"""Generate a PIN verifier without putting the PIN in shell history."""
import getpass
import re
from werkzeug.security import generate_password_hash

if __name__ == '__main__':
    pin = getpass.getpass('New five-digit coordinator PIN: ')
    if not re.fullmatch(r'[0-9]{5}', pin):
        raise SystemExit('PIN must contain exactly five ASCII digits.')
    if pin != getpass.getpass('Confirm PIN: '):
        raise SystemExit('PINs do not match.')
    print("CHAIN_PIN_HASH='" + generate_password_hash(pin, method='scrypt') + "'")
