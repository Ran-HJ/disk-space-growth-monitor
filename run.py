import os

from disk_monitor.ui import main
from disk_monitor.single_instance import SingleInstance, show_already_running_message


if __name__ == "__main__":
    instance_name = os.environ.get(
        "DISK_GROWTH_MONITOR_INSTANCE_NAME", "Local\\DiskGrowthMonitor"
    )
    instance = SingleInstance(instance_name)
    if not instance.acquire():
        show_already_running_message()
        raise SystemExit(0)
    try:
        main()
    finally:
        instance.release()
