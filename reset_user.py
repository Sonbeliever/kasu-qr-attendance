from werkzeug.security import generate_password_hash
import json

users = {
    "admin": {
        "password": generate_password_hash("admin123"),
        "role": "admin"
    },
    "student": {
        "password": generate_password_hash("student123"),
        "role": "student"
    }
}

with open("users.json", "w") as f:
    json.dump(users, f, indent=2)

print("New users.json created. Login with admin123 / student123.")
