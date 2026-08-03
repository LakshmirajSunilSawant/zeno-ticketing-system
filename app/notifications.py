# WHY this is a deliberate stub, not a missing feature:
# Sending email/SMS is a slow, failure-prone call to a third party. Doing it inline inside the
# booking transaction would (a) hold the row lock open for the duration of an HTTP call to
# SendGrid/Twilio, throttling every other booking for that event, and (b) fail the booking if the
# provider is down. The production shape is: commit the booking, publish a `booking.confirmed`
# event to a queue (SQS/Kafka), and let a separate worker deliver the notification with retries and
# a dead-letter queue. This module is the seam where that publish call would go - swapping the log
# line for `queue.publish(...)` is the whole change.
from app.logging_config import get_logger

log = get_logger("notifications")


def notify_booking_confirmed(booking) -> None:
    log.info(
        "notification_stub",
        channel="email",
        template="booking_confirmed",
        booking_id=booking.id,
        user_id=booking.user_id,
        seats=booking.seats,
    )


def notify_booking_cancelled(booking) -> None:
    log.info(
        "notification_stub",
        channel="email",
        template="booking_cancelled",
        booking_id=booking.id,
        user_id=booking.user_id,
    )
