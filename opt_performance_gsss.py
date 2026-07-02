#!/usr/bin/env python3
# filepath: /usr/local/bin/raid_manager.py

import subprocess
import sys
import os
import json
import time
import signal
import datetime
import tempfile
from typing import List, Tuple, Optional, Dict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ==================== CONFIGURATION ====================
CONFIG = {
    "json_output": "/var/log/raid_performance.json",
    "test_duration_hours": 10,
    "wait_between_tests": 60,
    "raid_md": "/dev/md0",
    "vg_name": "vg_raid",
    "lv_name": "lv_raid",
    "mount_point": "/mnt/raid_storage",
    "min_test_size_gb": 0.5,
}

# ==================== UTILITY FUNCTIONS ====================

def run_command(cmd: str, ignore_errors: bool = False, timeout: int = None) -> Tuple[int, str, str]:
    try:
        if timeout:
            process = subprocess.run(
                cmd, shell=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=timeout
            )
        else:
            process = subprocess.run(
                cmd, shell=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True
            )

        if not ignore_errors and process.returncode != 0:
            print(f"[ERROR] Command failed: {cmd}\n[ERROR] {process.stderr.strip()}")
        return process.returncode, process.stdout.strip(), process.stderr.strip()
    except subprocess.TimeoutExpired:
        print(f"[ERROR] Command timed out: {cmd}")
        return -1, "", "Timeout"
    except Exception as e:
        print(f"[ERROR] Exception: {e}")
        return -1, "", str(e)

def check_root():
    if os.geteuid() != 0:
        print("[ERROR] This script must be run as root (sudo).")
        sys.exit(1)

def confirm_action(message: str) -> bool:
    try:
        return input(f"\n{message} [yes/no]: ").strip().lower() in ("yes", "y")
    except KeyboardInterrupt:
        return False

def wait_for_raid_sync(md_device: str, max_wait_seconds: int = 10) -> bool:
    """Wait for the RAID device to appear, then show resync ETA or 'fully synced'."""
    print(f"[INFO] Waiting for {md_device} to appear...")
    
    # Wait for the block device node
    for attempt in range(max_wait_seconds):
        if os.path.exists(md_device):
            print(f"[INFO] {md_device} found.")
            break
        time.sleep(1)
    else:
        print(f"[ERROR] {md_device} did not appear after {max_wait_seconds} seconds.")
        return False
    
    # Give a moment for /proc/mdstat to be updated
    time.sleep(1)
    
    # Get base name for matching (e.g., "md0" from "/dev/md0")
    md_name = os.path.basename(md_device)
    ret, output, _ = run_command(f"cat /proc/mdstat", ignore_errors=True)
    
    if md_name in output:
        lines = output.split('\n')
        for line in lines:
            if md_name in line:
                if "resync" in line or "recovery" in line:
                    # Extract and print only the finish time
                    import re
                    match = re.search(r'finish=([\d.]+(?:min|s|h))', line)
                    if match:
                        print(f"[INFO] Resync ETA: {match.group(1)}")
                    else:
                        print(f"[INFO] {line.strip()}")
                    print("[INFO] Resync continues in background. Proceeding with setup.")
                else:
                    # No resync active – array is fully synced
                    print(f"[INFO] {line.strip()} (fully synced)")
                break
        else:
            # Should not happen because md_name was found, but just in case
            print(f"[INFO] RAID array {md_name} is active.")
    else:
        # Device node exists but not yet in mdstat (rare, but possible immediately after creation)
        print(f"[WARNING] {md_name} not yet in /proc/mdstat, but device node exists. Continuing.")
    
    return True
def get_disk_size_sectors(disk: str) -> int:
    try:
        dev_name = os.path.basename(disk)
        with open(f"/sys/block/{dev_name}/size", 'r') as f:
            return int(f.read().strip())
    except:
        return 0

def is_disk_in_use(disk: str) -> bool:
    dev_name = os.path.basename(disk)
    sysfs_path = Path(f"/sys/block/{dev_name}")

    # Check for partitions (any entry named dev_name + digit)
    try:
        if any(p.is_dir() and p.name.startswith(dev_name) and p.name != dev_name
               for p in sysfs_path.iterdir()):
            return True
    except Exception:
        pass

    # Check RAID membership (holders directory)
    holders_dir = sysfs_path / "holders"
    try:
        if holders_dir.exists() and any(holders_dir.iterdir()):
            return True
    except Exception:
        pass

    return False

def quick_clean_disk_fast(disk: str):
    run_command(f"mdadm --zero-superblock {disk}", ignore_errors=True)
    run_command(f"wipefs -a {disk}", ignore_errors=True)
    run_command(f"pvremove -ff -y {disk}", ignore_errors=True)
    run_command(f"dd if=/dev/zero of={disk} bs=1M count=1 status=none", ignore_errors=True)
    run_command(f"blockdev --rereadpt {disk}", ignore_errors=True)

# ==================== DISK DETECTION ====================

def list_available_disks() -> List[dict]:
    available = []
    try:
        for dev in Path('/sys/block').iterdir():
            dev_name = dev.name
            # Only consider common disk name prefixes
            if not dev_name.startswith(('sd', 'nvme', 'vd', 'hd')):
                continue

            dev_path = f"/dev/{dev_name}"

            # Skip if the disk is in use
            if is_disk_in_use(dev_path):
                continue

            # Get size in GB
            size_sectors = get_disk_size_sectors(dev_path)
            if size_sectors == 0:
                continue
            size_gb = (size_sectors * 512) // (1024 ** 3)

            # Check if SSD
            is_ssd = False
            try:
                with open(f"/sys/block/{dev_name}/queue/rotational", 'r') as f:
                    if f.read().strip() == '0':
                        is_ssd = True
            except:
                pass

            available.append({
                "path": dev_path,
                "size_gb": size_gb,
                "model": dev_name.upper(),
                "is_ssd": is_ssd
            })
    except Exception as e:
        print(f"[WARNING] Disk detection error: {e}")

    return available

def select_disks(available: List[dict], count: int, purpose: str, exclude_list: List[str] = None) -> List[str]:
    if exclude_list is None:
        exclude_list = []

    filtered = [d for d in available if d['path'] not in exclude_list]

    if len(filtered) < count:
        print(f"[ERROR] Not enough available disks")
        return []

    print(f"\n{'='*90}\n  Select {count} disk(s) for {purpose}\n{'='*90}")
    for idx, d in enumerate(filtered, 1):
        ssd_tag = " [SSD]" if d.get('is_ssd', False) else ""
        print(f"  {idx:<4} {d['path']:<12} {d['size_gb']}GB {d['model']}{ssd_tag}")

    selected = []
    while len(selected) < count:
        try:
            choice = int(input(f"Select disk #{len(selected)+1} [1-{len(filtered)}]: "))
            if 1 <= choice <= len(filtered):
                path = filtered[choice-1]["path"]
            else:
                print(f"Invalid choice.")
        except ValueError:
            print("Invalid input.")
        except:
            print("Invalid choice.")
    return selected

# ==================== CORE OPERATIONS ====================

def create_raid_instant(disks: List[str], level: str, md_device: str) -> bool:
    print(f"\n[INFO] Creating RAID {level} with {len(disks)} disks...")

    # Calculate total raw capacity
    total_gb = 0
    for d in disks:
        sectors = get_disk_size_sectors(d)
        total_gb += (sectors * 512) // (1024 ** 3)
    print(f"[INFO] Total raw capacity: {total_gb}GB")

    # Decide whether to use --assume-clean (safe only for RAID0/1/10)
    assume_clean_flag = ""
    if level in ("0", "1", "10"):
        assume_clean_flag = "--assume-clean"
        print("[INFO] Using --assume-clean (safe for RAID0/1/10)")
    else:
        print("[WARNING] RAID5/6 requires parity calculation. Array will start a background resync.")
        print("[WARNING] The array is usable immediately, but fault tolerance will be ready after resync.")

    # Build command
    cmd = (f"mdadm --create {md_device} --level={level} --raid-devices={len(disks)} "
           f"--metadata=1.2 {assume_clean_flag} --run --force {' '.join(disks)}")

    ret, _, stderr = run_command(cmd)
    if ret == 0:
        run_command("udevadm settle", ignore_errors=True)
        run_command("mdadm --detail --scan >> /etc/mdadm/mdadm.conf", ignore_errors=True)
        print(f"[SUCCESS] RAID {level} created.")

        # For RAID5/6, wait for initial stabilization (resync continues in background)
        if level in ("5", "6"):
            wait_for_raid_sync(md_device, max_wait_seconds=60)
        else:
            # For RAID0/1/10, wait briefly for metadata to settle
            wait_for_raid_sync(md_device, max_wait_seconds=30)
        
        return True

    print(f"[ERROR] RAID creation failed: {stderr}")
    return False

def setup_lvm_with_cache(md_device: str, cache_disk: Optional[str], vg_name: str, lv_name: str) -> Optional[str]:
    print("[INFO] Setting up LVM & XFS Filesystem...")

    # Create PV on RAID device (RAID should be stable from wait_for_raid_sync)
    print(f"[INFO] Creating physical volume on {md_device}...")
    ret, _, stderr = run_command(f"pvcreate -f -y {md_device}")
    if ret != 0:
        print(f"[ERROR] Failed to create PV: {stderr}")
        return None

    # Create VG
    ret, _, stderr = run_command(f"vgcreate -y {vg_name} {md_device}")
    if ret != 0:
        print(f"[ERROR] Failed to create VG: {stderr}")
        return None

    lv_path = f"/dev/{vg_name}/{lv_name}"

    if cache_disk:
        print(f"[INFO] Setting up cache on {cache_disk}")

        # Create PV on cache disk
        ret, _, stderr = run_command(f"pvcreate -f -y {cache_disk}")
        if ret != 0:
            print(f"[ERROR] Failed to create PV on cache: {stderr}")
            return None

        # Extend VG to include cache disk
        ret, _, stderr = run_command(f"vgextend {vg_name} {cache_disk}")
        if ret != 0:
            print(f"[ERROR] Failed to extend VG: {stderr}")
            return None

        # 1. Create data LV ONLY on the RAID PV
        ret, _, stderr = run_command(
            f"lvcreate -y -l 100%FREE --alloc cling -n {lv_name} {vg_name} {md_device}"
        )
        if ret != 0:
            print(f"[ERROR] Failed to create data LV: {stderr}")
            return None

        # 2. Create cache pool LV as type 'cache-pool' ONLY on the cache PV
        ret, _, stderr = run_command(
            f"lvcreate -y --type cache-pool -l 100%FREE --alloc cling -n cache_pool {vg_name} {cache_disk}"
        )
        if ret != 0:
            print(f"[ERROR] Failed to create cache pool: {stderr}")
            return None

        # 3. Attach cache pool to data LV
        ret, _, stderr = run_command(
            f"lvconvert --yes --type cache --cachepool {vg_name}/cache_pool --cachemode writeback {lv_path}"
        )
        if ret != 0:
            # Fallback: try --cachevol (newer LVM)
            ret, _, stderr = run_command(
                f"lvconvert --yes --type cache --cachevol {vg_name}/cache_pool --cachemode writeback {lv_path}"
            )
            if ret != 0:
                print(f"[ERROR] Failed to attach cache: {stderr}")
                return None

        print(f"[SUCCESS] Cache attached to {lv_path}")
    else:
        # No cache – create LV on the VG (all PVs)
        ret, _, stderr = run_command(f"lvcreate -y -l 100%FREE -n {lv_name} {vg_name}")
        if ret != 0:
            print(f"[ERROR] Failed to create LV: {stderr}")
            return None

    # Format with XFS
    ret, _, stderr = run_command(f"mkfs.xfs -f {lv_path}")
    if ret != 0:
        print(f"[ERROR] Failed to format XFS: {stderr}")
        return None

    print(f"[SUCCESS] XFS filesystem created")
    return lv_path

def run_single_fio_test(mount_point: str, test_config: dict) -> dict:
    result = {"error": None, "data": {}}

    # Create test directory
    test_dir = os.path.join(mount_point, "fio_test")
    os.makedirs(test_dir, exist_ok=True)

    # Use a temporary file for JSON output
    with tempfile.NamedTemporaryFile(mode='w+', suffix='.json', delete=False) as tmp:
        json_output_file = tmp.name

    try:
        # Build optimized FIO command with JSON output to file
        cmd = (f"fio --name={test_config['name']} --directory={test_dir} "
               f"--size={test_config['size']} --rw={test_config['rw']} "
               f"--bs={test_config['bs']} --numjobs={test_config['numjobs']} "
               f"--iodepth={test_config['iodepth']} --direct=1 ")

        # Add rwmixread if present
        if 'rwmixread' in test_config:
            cmd += f"--rwmixread={test_config['rwmixread']} "

        cmd += f"--runtime={test_config['runtime']} --time_based --group_reporting "
        cmd += f"--output-format=json --output={json_output_file}"

        ret, _, stderr = run_command(cmd, ignore_errors=True, timeout=test_config['runtime'] + 60)

        if ret != 0:
            result["error"] = f"FIO failed: {stderr[:200]}"
        else:
            # Parse JSON from file
            try:
                with open(json_output_file, 'r') as f:
                    fio_json = json.load(f)

                if fio_json.get('jobs') and len(fio_json['jobs']) > 0:
                    job = fio_json['jobs'][0]

                    # Extract read stats
                    read_stats = job.get('read', {})
                    write_stats = job.get('write', {})

                    result["data"] = {
                        'read_bw': read_stats.get('bw', 0),
                        'read_iops': read_stats.get('iops', 0),
                        'read_lat': read_stats.get('lat_ns', {}).get('mean', 0) / 1000 if read_stats.get('lat_ns') else 0,
                        'write_bw': write_stats.get('bw', 0),
                        'write_iops': write_stats.get('iops', 0),
                        'write_lat': write_stats.get('lat_ns', {}).get('mean', 0) / 1000 if write_stats.get('lat_ns') else 0
                    }
                else:
                    result["error"] = "No job data in FIO output"

            except json.JSONDecodeError as e:
                result["error"] = f"Invalid JSON output: {e}"
            except Exception as e:
                result["error"] = f"Failed to parse FIO output: {e}"

    finally:
        # Cleanup
        try:
            os.unlink(json_output_file)
        except:
            pass


    return result

def run_extended_fio_test(mount_point: str, json_file: str, duration_hours: int, wait_seconds: int):
    print(f"\n[INFO] Starting extended {duration_hours}-hour performance test...")
    print(f"[INFO] Tests will run every {wait_seconds} seconds")
    print(f"[INFO] Results will be saved to: {json_file}")

    # Check if FIO is installed
    ret, _, _ = run_command("which fio", ignore_errors=True)
    if ret != 0:
        print("[INFO] FIO is not installed. Installing...")
        run_command("apt-get update && apt-get install -y fio", ignore_errors=True)
        time.sleep(2)

    # Check mount point
    if not os.path.exists(mount_point):
        print(f"[ERROR] Mount point {mount_point} does not exist")
        return

    # Get available space
    free_space_gb = 0
    try:
        stat = os.statvfs(mount_point)
        free_space_gb = (stat.f_frsize * stat.f_bavail) / (1024 ** 3)
        print(f"[INFO] Available space: {free_space_gb:.1f}GB")
    except:
        pass

    # FIO test configurations - optimized for speed
    test_configs = [
        {"name": "randread_4k", "size": "200M", "rw": "randread", "bs": "4k",
         "numjobs": 4, "iodepth": 32, "runtime": 10},
        {"name": "randwrite_4k", "size": "200M", "rw": "randwrite", "bs": "4k",
         "numjobs": 4, "iodepth": 32, "runtime": 10},
        {"name": "read_1m", "size": "500M", "rw": "read", "bs": "1M",
         "numjobs": 2, "iodepth": 16, "runtime": 10},
        {"name": "write_1m", "size": "500M", "rw": "write", "bs": "1M",
         "numjobs": 2, "iodepth": 16, "runtime": 10},
        {"name": "randrw_70_30_4k", "size": "200M", "rw": "randrw", "bs": "4k",
         "numjobs": 4, "iodepth": 32, "runtime": 10, "rwmixread": 70}
    ]

    # Performance data structure
    performance_data = {
        "test_start": datetime.datetime.now().isoformat(),
        "test_duration_hours": duration_hours,
        "wait_between_tests_seconds": wait_seconds,
        "free_space_gb": free_space_gb,
        "raid_configuration": {},
        "results": []
    }

    # Get RAID configuration
    raid_config = {}
    ret, output, _ = run_command(f"mdadm --detail {CONFIG['raid_md']}", ignore_errors=True)
    if ret == 0:
        for line in output.split('\n'):
            if 'Raid Level' in line:
                raid_config['level'] = line.split(':')[1].strip()
            elif 'Array Size' in line:
                raid_config['size'] = line.split(':')[1].strip()
            elif 'Chunk Size' in line:
                raid_config['chunk_size'] = line.split(':')[1].strip()

    performance_data["raid_configuration"] = raid_config

    start_time = time.time()
    test_end_time = start_time + (duration_hours * 3600)
    test_counter = 0

    # Signal handler for graceful shutdown
    def signal_handler(sig, frame):
        print("\n[INFO] Stopping performance test...")
        performance_data["test_end"] = datetime.datetime.now().isoformat()
        performance_data["completed"] = False
        performance_data["total_tests"] = test_counter

        with open(json_file, 'w') as f:
            json.dump(performance_data, f, indent=2)
        print(f"[INFO] Performance data saved to {json_file}")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while time.time() < test_end_time:
        test_counter += 1
        test_timestamp = datetime.datetime.now().isoformat()

        print(f"\n{'='*90}")
        print(f"[TEST #{test_counter}] Running benchmark at {test_timestamp}")
        remaining_hours = max(0, (test_end_time - time.time())/3600)
        remaining_minutes = remaining_hours * 60
        print(f"[INFO] Remaining: {remaining_hours:.1f} hours ({remaining_minutes:.0f} minutes)")
        print(f"[INFO] Free space: {free_space_gb:.1f}GB")
        print(f"{'='*90}")

        test_results = {
            "timestamp": test_timestamp,
            "test_number": test_counter,
            "free_space_gb": free_space_gb,
            "tests": {}
        }

        # Run each test configuration
        for config in test_configs:
            print(f"\n[TEST] {config['name']}...", end="", flush=True)
            result = run_single_fio_test(mount_point, config)

            if result["error"]:
                print(f" ✗ {result['error']}")
                test_results["tests"][config['name']] = {"error": result["error"]}
            else:
                # Display results
                read_bw_mb = result['data'].get('read_bw', 0) / 1024
                write_bw_mb = result['data'].get('write_bw', 0) / 1024
                if read_bw_mb > 0:
                    print(f" ✓ Read: {read_bw_mb:.1f}MB/s, IOPS: {result['data'].get('read_iops', 0):.0f}")
                if write_bw_mb > 0:
                    print(f" ✓ Write: {write_bw_mb:.1f}MB/s, IOPS: {result['data'].get('write_iops', 0):.0f}")
                test_results["tests"][config['name']] = result["data"]

        # Update free space
        try:
            stat = os.statvfs(mount_point)
            free_space_gb = (stat.f_frsize * stat.f_bavail) / (1024 ** 3)
            test_results["free_space_gb"] = free_space_gb
        except:
            pass

        performance_data["results"].append(test_results)

        print(f"\n[INFO] Test #{test_counter} completed. Data saved to {json_file}")

        # Wait for next test interval
        if time.time() < test_end_time:
            print(f"\n[INFO] Waiting {wait_seconds} seconds before next test...")

            # Wait with progress indicator
            for remaining in range(wait_seconds, 0, -1):
                print(f"\r[WAIT] {remaining} seconds remaining...", end="", flush=True)
                time.sleep(1)
            print()  # New line after countdown

    # Test completed
    performance_data["test_end"] = datetime.datetime.now().isoformat()
    performance_data["completed"] = True
    performance_data["total_tests"] = test_counter

    with open(json_file, 'w') as f:
        json.dump(performance_data, f, indent=2)

    print(f"\n{'='*90}")
    print(f"[SUCCESS] Extended test completed!")
    print(f"[SUCCESS] Total tests run: {test_counter}")
    print(f"[SUCCESS] Performance data saved to: {json_file}")
    print(f"{'='*90}")

def display_raid_details(md_device, vg_name, lv_name, mount_point):
    print(f"\n{'='*90}\n  STORAGE DETAILS\n{'='*90}")

    print("\n[1] DISK USAGE:")
    _, out, _ = run_command(f"df -h {mount_point}", ignore_errors=True)
    print(out if out else "Not mounted")

    print("\n[2] RAID DETAIL:")
    _, out, _ = run_command(f"mdadm --detail {md_device}", ignore_errors=True)
    print(out if out else "Not active")

    print("\n[3] RAID STATUS:")
    _, out, _ = run_command("cat /proc/mdstat", ignore_errors=True)
    print(out if out else "No info")

    print("\n[4] CACHE STATUS:")
    _, out, _ = run_command(f"lvs -a {vg_name} -o name,attr,cache_mode", ignore_errors=True)
    print(out if out else "No cache")



def cleanup(md_device: str, vg_name: str, lv_name: str, mount_point: str):
    if not confirm_action("DANGER: DESTROY ALL DATA ON RAID, CACHE, AND LVM?"):
        return
    
    print("[INFO] Starting FAST cleanup...")
    
    # 1. Unmount (lazy) – no sleep needed
    run_command(f"umount -l {mount_point}", ignore_errors=True)
    
  
    # 3. Remove LVM with --noudevsync to skip udev waits
    run_command(f"lvchange -an {vg_name}/{lv_name} --noudevsync", ignore_errors=True)
    run_command(f"lvremove -y -f {vg_name} --noudevsync", ignore_errors=True)
    run_command(f"vgremove -y -f {vg_name} --noudevsync", ignore_errors=True)
    run_command(f"pvremove -y -f {md_device} --noudevsync", ignore_errors=True)
    
    # 4. Get member disks (if RAID still active)
    all_disks = set()
    ret, output, _ = run_command(f"mdadm --detail {md_device} 2>/dev/null | grep -E '^[[:space:]]+[0-9]+' | awk '{{print $NF}}'", ignore_errors=True)
    if output:
        for disk in output.split('\n'):
            if disk and os.path.exists(disk):
                all_disks.add(disk)
    
    # Also get cache disk from LVM
    ret, output, _ = run_command(f"pvs 2>/dev/null | grep -v '{os.path.basename(md_device)}' | grep '/dev/' | awk '{{print $1}}'", ignore_errors=True)
    if output:
        for disk in output.split('\n'):
            if disk and disk != md_device and os.path.exists(disk):
                all_disks.add(disk)
    
    # 5. Force stop RAID – no sleep
    md_name = os.path.basename(md_device)
    run_command(f"echo 'clear' > /sys/block/{md_name}/md/array_state", ignore_errors=True)
    run_command(f"mdadm --stop {md_device} --force", ignore_errors=True)
    
    # 6. Clean disks in parallel
    if not all_disks:
        # Fallback: scan all block devices
        ret, output, _ = run_command("lsblk -d -n -o NAME | grep -E '^(sd|nvme|vd|hd)'", ignore_errors=True)
        if output:
            for disk_name in output.split('\n'):
                if disk_name:
                    disk_path = f"/dev/{disk_name}"
                    if disk_path != md_device and os.path.exists(disk_path):
                        all_disks.add(disk_path)
    
    if all_disks:
        print(f"[INFO] Cleaning {len(all_disks)} disk(s) in parallel...")
        with ThreadPoolExecutor(max_workers=min(8, len(all_disks))) as executor:
            executor.map(quick_clean_disk_fast, all_disks)
    else:
        print("[INFO] No disks found to clean.")
    
    # 7. Clean RAID config (only if file exists)
    if os.path.exists("/etc/mdadm/mdadm.conf"):
        run_command("sed -i '/md0/d' /etc/mdadm/mdadm.conf", ignore_errors=True)
        # Only rebuild initramfs if we actually changed the config
        if os.path.exists("/usr/bin/dracut"):
            run_command("dracut --force --quiet", ignore_errors=True)
        elif os.path.exists("/usr/sbin/update-initramfs"):
            run_command("update-initramfs -u", ignore_errors=True)
    
    # 8. Remove mount point if empty
    try:
        if os.path.exists(mount_point) and not os.listdir(mount_point):
            os.rmdir(mount_point)
    except:
        pass
    
    # 9. Restart udisks2
    run_command("systemctl start udisks2", ignore_errors=True)
    
    print("[SUCCESS] Cleanup finished in record time!")
    print(f"[INFO] Cleaned disks: {', '.join(all_disks) if all_disks else 'None'}")
# ==================== MAIN WORKFLOW ====================

def full_setup():
    print("\n[INFO] Detecting available disks...")
    available = list_available_disks()

    if len(available) < 2:
        print("[ERROR] At least 2 disks required.")
        return

    print(f"\n[INFO] Available disks:")
    for d in available:
        ssd_tag = " [SSD]" if d.get('is_ssd', False) else ""
        print(f"  {d['path']:<12} {d['size_gb']}GB {d['model']}{ssd_tag}")

    level = input("\nSelect RAID Level (0, 1, 5, 6, 10): ").strip()
    min_map = {'0': 2, '1': 2, '5': 3, '6': 4, '10': 4}
    min_d = min_map.get(level)

    if not min_d or len(available) < min_d:
        print("[ERROR] Invalid RAID level or not enough disks.")
        return

    # Ask how many disks to use
    max_d = len(available)
    while True:
        try:
            disk_count = int(input(f"How many disks do you want to use for RAID{level}? (minimum {min_d}, available {max_d}): "))
            if disk_count < min_d:
                print(f"At least {min_d} disks required for RAID{level}.")
            elif disk_count > max_d:
                print(f"Only {max_d} disks available.")
            else:
                break
        except ValueError:
            print("Please enter a valid number.")

    # Select data disks
    data_disks = select_disks(available, disk_count, f"RAID{level} Array", exclude_list=[])
    if not data_disks:
        return

    

    if not confirm_action("Proceed with setup? This will DESTROY ALL DATA on selected disks."):
        return

    start_time = time.time()

    if not create_raid_instant(data_disks, level, CONFIG['raid_md']):
        print("[ERROR] RAID creation failed.")
        return

    # RAID sync wait is handled in create_raid_instant, proceed with LVM
    print("[INFO] RAID is stable. Proceeding with LVM setup...")
    lv_path = setup_lvm_with_cache(CONFIG['raid_md'], cache_disk, CONFIG['vg_name'], CONFIG['lv_name'])
    if not lv_path:
        print("[ERROR] LVM setup failed.")
        return

    os.makedirs(CONFIG['mount_point'], exist_ok=True)
    ret, _, stderr = run_command(f"mount {lv_path} {CONFIG['mount_point']}")
    if ret != 0:
        print(f"[ERROR] Failed to mount: {stderr}")
        return

    run_command(f"chmod 777 {CONFIG['mount_point']}", ignore_errors=True)

    setup_time = time.time() - start_time
    print(f"\n[SUCCESS] Setup completed in {setup_time:.2f} seconds!")

    display_raid_details(CONFIG['raid_md'], CONFIG['vg_name'], CONFIG['lv_name'], CONFIG['mount_point'])

    if confirm_action("\nRun extended performance test now?"):
        run_extended_fio_test(
            CONFIG['mount_point'],
            CONFIG['json_output'],
            CONFIG['test_duration_hours'],
            CONFIG['wait_between_tests']
        )

if __name__ == "__main__":
    check_root()

    if len(sys.argv) > 1:
        if sys.argv[1] == "cleanup":
            cleanup(CONFIG['raid_md'], CONFIG['vg_name'], CONFIG['lv_name'], CONFIG['mount_point'])
            sys.exit(0)
        elif sys.argv[1] == "status":
            display_raid_details(CONFIG['raid_md'], CONFIG['vg_name'], CONFIG['lv_name'], CONFIG['mount_point'])
            sys.exit(0)
        elif sys.argv[1] == "test":
            if os.path.exists(CONFIG['mount_point']):
                run_extended_fio_test(
                    CONFIG['mount_point'],
                    CONFIG['json_output'],
                    CONFIG['test_duration_hours'],
                    CONFIG['wait_between_tests']
                )
            else:
                print(f"[ERROR] Mount point not found")
            sys.exit(0)

    # Interactive menu
    while True:
        print(f"\n{'='*90}")
        print("  RAID MANAGER - INSTANT MODE")
        print(f"{'='*90}")
        print("1. Full Setup (Instant RAID + XFS + Test)")
        print("2. Display Details & Status")
        print("3. Run Extended Performance Test Only")
        print("4. Cleanup & Destroy ALL Data (RAID + Cache)")
        print("5. Exit")

        choice = input("\nSelect option: ").strip()

        if choice == "1":
            full_setup()
        elif choice == "2":
            display_raid_details(CONFIG['raid_md'], CONFIG['vg_name'], CONFIG['lv_name'], CONFIG['mount_point'])
        elif choice == "3":
            if os.path.exists(CONFIG['mount_point']):
                run_extended_fio_test(
                    CONFIG['mount_point'],
                    CONFIG['json_output'],
                    CONFIG['test_duration_hours'],
                    CONFIG['wait_between_tests']
                )
            else:
                print(f"[ERROR] {CONFIG['mount_point']} not mounted")
        elif choice == "4":
            cleanup(CONFIG['raid_md'], CONFIG['vg_name'], CONFIG['lv_name'], CONFIG['mount_point'])
        elif choice == "5":
            break
