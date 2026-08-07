from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

class Command(BaseCommand):
    help = 'Creates the horilla user for WorkRadar integration'

    def handle(self, *args, **options):
        username = 'horilla'
        password = 'Horilla@123'
        email = 'horilla@example.com'

        if User.objects.filter(username=username).exists():
            self.stdout.write(self.style.SUCCESS(f'User "{username}" already exists.'))
            # Optionally update password if needed
            user = User.objects.get(username=username)
            user.set_password(password)
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Password for user "{username}" has been updated.'))
        else:
            User.objects.create_user(username=username, password=password, email=email)
            self.stdout.write(self.style.SUCCESS(f'Successfully created user "{username}"'))
