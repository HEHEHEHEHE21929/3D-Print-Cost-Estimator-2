import smtplib
import os
from email.message import EmailMessage

def send_order_email(order, file_path):
    # Set up email details
    sender = "your_email@gmail.com"  # Replace with your Gmail address
    recipient = "info@techtribeunlimited.com"
    subject = f"New 3D Print Order from {order['customer_name']}"
    body = f"""
New 3D Print Order:

Name: {order['customer_name']}
Email: {order['customer_email']}
Model: {order['file']}
Infill: {order['infill']}%
Wall Thickness: {order['wall_thickness']} mm
Estimated Print Time: {order['time']}
Estimated Cost: ${order['cost']}

G-code Path: {order['gcode_path']}
"""

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    # Attach the uploaded file
    if file_path and os.path.isfile(file_path):
        with open(file_path, "rb") as f:
            file_data = f.read()
            file_name = os.path.basename(file_path)
        msg.add_attachment(file_data, maintype="application", subtype="octet-stream", filename=file_name)

    # Send the email (using Gmail SMTP)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender, "your_app_password")  # Replace with your Gmail app password
            smtp.send_message(msg)
    except Exception as e:
        print(f"Failed to send email: {e}")
