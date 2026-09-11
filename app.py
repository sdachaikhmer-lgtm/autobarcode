import os
import re
import datetime
import csv
import shutil
import json
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox, simpledialog, ttk
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageEnhance
import requests
from io import BytesIO
import concurrent.futures
import time
import threading
import subprocess
import sys
import gspread

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/drive']

duplicate_lock = threading.Lock()
CONFIG_FILE = "app_config.json"
TARGET_PARENT_FOLDER_ID = "1sTeOcK79ytlePV0zF84zFKA2Q52t82pT"

def save_config(data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception:
        pass

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def draw_wrapped_text(draw, text, font, max_width):
    lines = []
    words = text.split(' ')
    current_line = ""
    for word in words:
        bbox = draw.textbbox((0, 0), word, font=font)
        if (bbox[2] - bbox[0]) > max_width:
            if current_line: lines.append(current_line); current_line = ""
            sub_word = ""
            for char in word:
                test_sub = sub_word + char
                if (draw.textbbox((0, 0), test_sub, font=font)[2] - draw.textbbox((0, 0), test_sub, font=font)[0]) <= max_width:
                    sub_word = test_sub
                else: lines.append(sub_word); sub_word = char
            if sub_word: current_line = sub_word
            continue
        test_line = current_line + " " + word if current_line else word
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if (bbox[2] - bbox[0]) <= max_width: current_line = test_line
        else:
            if current_line: lines.append(current_line)
            current_line = word
    if current_line: lines.append(current_line)
    return lines

def is_file_ready(file_path):
    if not os.path.exists(file_path):
        return False
    try:
        size1 = os.path.getsize(file_path)
        if size1 <= 0:
            return False
        time.sleep(0.1)
        size2 = os.path.getsize(file_path)
        if size1 != size2:
            return False
        with open(file_path, 'ab'):
            pass
        return True
    except Exception:
        return True

def log_error_to_file(error_msg):
    try:
        with open("error_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n")
    except Exception:
        pass

def get_today_active_date_folder():
    today_str = datetime.datetime.now().strftime("%d-%b-%Y")
    return rf"D:\Scan\Main OutputFastOCR\OutputFastOCR_{today_str}"

def process_single_file(file_name, source_dir, output_dir, fixed_img, copy_only=False, seen_containers=None, first_seen_files=None, verify_mode=False, register_numbers=None):
    source_file_path = os.path.join(source_dir, file_name)
    output_file_path = os.path.join(output_dir, file_name)
    
    for _ in range(10):
        if is_file_ready(source_file_path):
            break
        time.sleep(0.3)
    else:
        return ("ignored", None)

    is_image = file_name.lower().endswith(('.jpg', '.jpeg', '.png'))
    if not is_image:
        try:
            if os.path.exists(source_file_path):
                if copy_only:
                    shutil.copy2(source_file_path, output_file_path)
                else:
                    shutil.move(source_file_path, output_file_path)
                return ("processed", f"Passed Non-Image File: {file_name}")
        except Exception as e:
            err_str = f"Error passing file {file_name}: {e}"
            log_error_to_file(err_str)
            return ("error", err_str)
        return ("ignored", None)

    def force_move_to_error():
        error_folder = os.path.join(output_dir, "Error_Files")
        os.makedirs(error_folder, exist_ok=True)
        error_path = os.path.join(error_folder, file_name)
        for _ in range(3):
            try:
                if os.path.exists(source_file_path):
                    shutil.move(source_file_path, error_path)
                    return True
            except Exception:
                try:
                    shutil.copy2(source_file_path, error_path)
                    if os.path.exists(source_file_path):
                        os.remove(source_file_path)
                    return True
                except Exception:
                    time.sleep(0.3)
        return False

    if "@" not in file_name:
        force_move_to_error()
        err_str = f"Moved Unidentified File to Error_Files (Missing '@'): {file_name}"
        log_error_to_file(err_str)
        return ("error", err_str)

    try:
        before_at, after_at = file_name.split("@", 1)
        number_matches = re.findall(r'\d+', after_at)
        if not number_matches:
            number_matches = re.findall(r'\d+', file_name)
            
        container_unique_key = after_at.strip().split('.')[0].upper()

        if not number_matches:
            force_move_to_error()
            err_str = f"Moved Unidentified File to Error_Files (No numbers): {file_name}"
            log_error_to_file(err_str)
            return ("error", err_str)

        barcode_text = number_matches[0]
        matched_main_filename = None

        if verify_mode:
            match_e = re.search(r'E\s*(\d+)', file_name, re.IGNORECASE)
            incoming_reg = match_e.group(1) if match_e else None

            matched_reg = None
            if incoming_reg and register_numbers and incoming_reg in register_numbers:
                matched_reg = incoming_reg
                matched_main_filename = register_numbers[incoming_reg]
            else:
                all_file_numbers = re.findall(r'\d+', file_name)
                for num in all_file_numbers:
                    if register_numbers and num in register_numbers:
                        matched_reg = num
                        matched_main_filename = register_numbers[num]
                        break

            if not matched_reg or not matched_main_filename:
                mismatch_folder = os.path.join(output_dir, "Unverified_Registers")
                os.makedirs(mismatch_folder, exist_ok=True)
                mismatch_path = os.path.join(mismatch_folder, file_name)
                if copy_only:
                    shutil.copy2(source_file_path, mismatch_path)
                else:
                    shutil.move(source_file_path, mismatch_path)
                return ("unverified", f"Skipped (Verify Mismatch): Moved to Unverified_Registers | {file_name}")

            if seen_containers is not None and first_seen_files is not None:
                with duplicate_lock:
                    if matched_reg in seen_containers:
                        dup_folder = os.path.join(output_dir, "Duplicate_containers")
                        os.makedirs(dup_folder, exist_ok=True)
                        dup_file_path = os.path.join(dup_folder, file_name)
                        report_path = os.path.join(dup_folder, "duplicates_report.csv")
                        try:
                            file_exists = os.path.exists(report_path)
                            with open(report_path, mode='a', newline='', encoding='utf-8') as f:
                                writer = csv.writer(f)
                                if not file_exists: 
                                    writer.writerow(["Timestamp", "Duplicate File", "Register Number", "Original File"])
                                writer.writerow([
                                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                                    file_name, 
                                    matched_reg, 
                                    first_seen_files.get(matched_reg, "Unknown")
                                ])
                        except:
                            pass
                        if copy_only:
                            shutil.copy2(source_file_path, dup_file_path)
                        else:
                            shutil.move(source_file_path, dup_file_path)
                        return ("duplicate_container", f"Skipped Duplicate Register ({matched_reg}) | {file_name}")
                    else:
                        seen_containers.add(matched_reg)
                        if matched_reg not in first_seen_files:
                            first_seen_files[matched_reg] = file_name

        else:
            if seen_containers is not None and first_seen_files is not None:
                with duplicate_lock:
                    if container_unique_key in seen_containers:
                        dup_folder = os.path.join(output_dir, "Duplicate_containers")
                        os.makedirs(dup_folder, exist_ok=True)
                        dup_file_path = os.path.join(dup_folder, file_name)
                        report_path = os.path.join(dup_folder, "duplicates_report.csv")
                        try:
                            file_exists = os.path.exists(report_path)
                            with open(report_path, mode='a', newline='', encoding='utf-8') as f:
                                writer = csv.writer(f)
                                if not file_exists: 
                                    writer.writerow(["Timestamp", "Duplicate File", "Container Identifier", "Original File"])
                                writer.writerow([
                                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                                    file_name, 
                                    container_unique_key, 
                                    first_seen_files.get(container_unique_key, "Unknown")
                                ])
                        except:
                            pass
                        if copy_only:
                            shutil.copy2(source_file_path, dup_file_path)
                        else:
                            shutil.move(source_file_path, dup_file_path)
                        return ("duplicate_container", f"Skipped Duplicate Container ({container_unique_key}) | {file_name}")
                    else:
                        seen_containers.add(container_unique_key)
                        if container_unique_key not in first_seen_files:
                            first_seen_files[container_unique_key] = file_name

        output_file_path = os.path.join(output_dir, file_name)
        if os.path.exists(output_file_path):
            if not copy_only and os.path.exists(source_file_path):
                try:
                    os.remove(source_file_path)
                except:
                    pass
            return ("output_existing", None)

        only_digits = "".join(filter(str.isdigit, barcode_text))
        last_two = int(only_digits[-2:]) if len(only_digits) >= 2 else 0
        calculated_angle = last_two + 180 if last_two % 2 == 0 else last_two + 190
        if calculated_angle > 360: calculated_angle -= 360
        rotation_angle = float(calculated_angle)

        with Image.open(source_file_path) as img:
            base_img = img.convert("RGBA").copy()
            
        base_width, base_height = base_img.size
        
        barcode_url = f"https://barcodeapi.org/api/code128/{requests.utils.quote(barcode_text)}"
        response = requests.get(barcode_url, timeout=3)
        
        if response.status_code != 200:
            force_move_to_error()
            err_str = f"Error with {file_name}: API error code {response.status_code}"
            log_error_to_file(err_str)
            return ("error", err_str)

        barcode_img = Image.open(BytesIO(response.content)).convert("RGBA")
        barcode_img = barcode_img.resize((1000, 250))
        
        gray_barcode = ImageOps.grayscale(barcode_img)
        green_badge = Image.new("RGBA", barcode_img.size, (0, 150, 0, 255))
        barcode_img = Image.composite(green_badge, Image.new("RGBA", barcode_img.size, (255, 255, 255, 0)), ImageOps.invert(gray_barcode))

        spacing = 35  
        draw = ImageDraw.Draw(base_img)
        
        try:
            font = ImageFont.truetype("arial.ttf", 54)
        except IOError:
            font = ImageFont.load_default()

        if fixed_img:
            rotated_stamp = fixed_img.rotate(rotation_angle, expand=True, resample=Image.BICUBIC)
            fx_width, fx_height = rotated_stamp.size
            fx_center_x = (base_width - fx_width) // 2
            fx_center_y = (base_height - fx_height) // 2
            
            base_img.paste(rotated_stamp, (fx_center_x, fx_center_y), rotated_stamp)
            current_time_str = datetime.datetime.now().strftime("%A, %B %d, %Y at %H:%M:%S")
            
            time_bbox = draw.textbbox((0, 0), current_time_str, font=font)
            time_width = time_bbox[2] - time_bbox[0]
            time_height = time_bbox[3] - time_bbox[1]
            
            max_text_width = base_width - 200
            text_to_print = file_name
                
            filename_lines = draw_wrapped_text(draw, text_to_print, font, max_text_width)
            
            line_height = time_height + 10
            timestamp_y = fx_center_y - time_height - spacing
            
            bc_width, bc_height = barcode_img.size
            bc_x = (base_width - bc_width) // 2
            bc_y = fx_center_y + fx_height + spacing
            base_img.paste(barcode_img, (bc_x, bc_y), barcode_img)

            timestamp_bottom_y = int(base_height * 0.85)
            start_filename_y = timestamp_bottom_y + time_height + 35
            
            current_y = start_filename_y
            for line in filename_lines:
                line_bbox = draw.textbbox((0, 0), line, font=font)
                line_width = line_bbox[2] - line_bbox[0]
                line_x = (base_width - line_width) // 2
                draw.text((line_x, current_y), line, fill="red", font=font)
                current_y += line_height

            time_x = (base_width - time_width) // 2
            draw.text((time_x, timestamp_y), current_time_str, fill="red", font=font)

        base_img.convert("RGB").save(output_file_path, "JPEG", quality=90)
        base_img.close()
        
        if not copy_only and os.path.exists(source_file_path):
            try:
                archive_dir = os.path.join(source_dir, "Processed_Backup")
                os.makedirs(archive_dir, exist_ok=True)
                shutil.move(source_file_path, os.path.join(archive_dir, file_name))
            except:
                try:
                    os.remove(source_file_path)
                except:
                    pass
        
        action_label = "Copied & Processed" if copy_only else "Success & Moved"
        return ("processed", f"{action_label}: {file_name} | Barcode: {barcode_text}")
        
    except Exception as e:
        force_move_to_error()
        err_str = f"Moved Stuck/Corrupted File to Error_Files: {file_name} | Reason: {e}"
        log_error_to_file(err_str)
        return ("error", err_str)

class BarcodeApp:
    def run_full_automated_sequence(self):
        def log_msg(msg):
            print(msg)
            try:
                self.log_box.insert(tk.END, msg + "\n")
                self.log_box.see(tk.END)
            except Exception:
                pass

        log_msg("=== Starting Automated Saturday Sequence ===")
        try:
            log_msg("[Step 1/5] Running Auto_Create Folder...")
            self.auto_create_barcode_stamp_folder(silent=True)

            log_msg("[Step 2/5] Running Auto-Copy (list_of_container)...")
            source_dir = self.source_entry.get().strip()
            output_dir = self.output_entry.get().strip()
            if source_dir and output_dir and os.path.exists(source_dir):
                target_list_dir = os.path.join(output_dir, "list_of_container")
                os.makedirs(target_list_dir, exist_ok=True)
                for file_name in os.listdir(source_dir):
                    src_file_path = os.path.join(source_dir, file_name)
                    dest_file_path = os.path.join(target_list_dir, file_name)
                    if os.path.isfile(src_file_path) and not os.path.exists(dest_file_path):
                        if is_file_ready(src_file_path):
                            try:
                                shutil.copy2(src_file_path, dest_file_path)
                            except Exception:
                                pass

            log_msg("[Step 3/5] Running Auto-Create Gdrive Folder & Log IDs...")
            target_path = self.gdrive_src_entry.get().strip()
            parent_id = TARGET_PARENT_FOLDER_ID
            headers = self.get_oauth_headers()
            folder_base_name = os.path.basename(os.path.normpath(target_path))
            date_match = re.search(r'(\d{2}-\w{3}-\d{4})', folder_base_name)
            date_str = date_match.group(1) if date_match else datetime.datetime.now().strftime("%d-%b-%Y")
            c_name = f"CustomsDocs_{date_str}"
            
            gdrive_folder_id = self.get_or_create_folder_id(headers, parent_id, c_name)
            subfolder_ids_map = {}
            if gdrive_folder_id:
                for sub in ["BarCodeAndStamp", "Container List", "Container Match Format (VGM)", "Main", "Part", "Report"]:
                    sub_id = self.get_or_create_folder_id(headers, gdrive_folder_id, sub)
                    if sub_id:
                        subfolder_ids_map[sub] = sub_id
                self.log_folder_structure_to_sheet(date_str, c_name, gdrive_folder_id, subfolder_ids_map)

            log_msg("[Step 4/5] Running Auto-Sync CustomsDocs...")
            if gdrive_folder_id:
                self.sync_folder_by_id(target_path, gdrive_folder_id, subfolder_ids_map, headers)

            log_msg("[Step 5/5] Starting Auto-Watch & Process...")
            target_output_dir = self.output_entry.get().strip()
            if target_output_dir and os.path.exists(source_dir):
                os.makedirs(target_output_dir, exist_ok=True)
                os.makedirs(os.path.join(target_output_dir, "Error_Files"), exist_ok=True)
                fixed_img = self.load_stamp_image()
                all_files = [f for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))]
                valid_files = [f for f in all_files if "@" in f]
                other_files = [f for f in all_files if "@" not in f]
                container_files = valid_files + other_files
                
                if container_files:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                        futures = [
                            executor.submit(process_single_file, file_name, source_dir, target_output_dir, fixed_img, copy_only=False, seen_containers=self.seen_containers, first_seen_files=self.first_seen_files, verify_mode=self.verify_mode_active, register_numbers=self.register_numbers_set)
                            for file_name in container_files
                        ]
                        concurrent.futures.wait(futures)

            log_msg("=== Automated Sequence Completed Successfully ===")
        except Exception as e:
            err_str = f"ERROR during automated sequence: {e}"
            log_msg(err_str)
            log_error_to_file(err_str)

    def __init__(self, root):
        self.root = root
        self.root.title("High-Speed Scrollable Barcode App")
        self.root.geometry("1020x880")
        self.root.configure(bg="#f4f6f7")
        
        self.is_watching = False
        self.is_copy_processing = False  
        self.is_list_copy_looping = False  
        self.is_gdrive_folder_sync_looping = False  
        self.current_font_size = 11  
        self.session_output_dir = "" 
        self.is_log_expanded = False
        
        self.verify_mode_active = False 
        self.register_numbers_set = {} 
        
        self.seen_containers = set()
        self.first_seen_files = {}  
        self.processed_source_files = set()
        
        self.count_processed = 0
        self.count_already_stamped = 0
        self.count_files_move = 0
        self.count_errors = 0

        self.config_data = load_config()
        
        container_outer = tk.Frame(root, bg="#f4f6f7")
        container_outer.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(container_outer, bg="#f4f6f7", highlightthickness=0)
        self.scrollbar = tk.Scrollbar(container_outer, orient=tk.VERTICAL, command=self.canvas.yview)
        
        self.main_frame = tk.Frame(self.canvas, bg="#f4f6f7")
        
        self.main_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        
        self.canvas_window = self.canvas.create_window((0, 0), window=self.main_frame, anchor="nw")
        
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width)
        )

        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)
        
        inner_pad = tk.Frame(self.main_frame, bg="#f4f6f7")
        inner_pad.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        top_control_frame = tk.Frame(inner_pad, bg="#f4f6f7")
        top_control_frame.pack(fill=tk.X, pady=(0, 10))
        
        left_ctrls = tk.Frame(top_control_frame, bg="#f4f6f7")
        left_ctrls.pack(side=tk.LEFT)
        
        self.reset_btn = tk.Button(left_ctrls, text=" 🔄 Clear Log ", bg="#f39c12", fg="white", font=("Arial", 10, "bold"), bd=0, relief=tk.FLAT, padx=8, pady=5, command=self.reset_history)
        self.reset_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.jump_error_btn = tk.Button(left_ctrls, text=" ⚠️ Jump to Error ", bg="#e74c3c", fg="white", font=("Arial", 10, "bold"), bd=0, relief=tk.FLAT, padx=8, pady=5, command=self.jump_to_error_log)
        self.jump_error_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.expand_log_btn = tk.Button(left_ctrls, text=" 📜 Expand Log ", bg="#34495e", fg="white", font=("Arial", 10, "bold"), bd=0, relief=tk.FLAT, padx=8, pady=5, command=self.toggle_expand_log)
        self.expand_log_btn.pack(side=tk.LEFT)

        zoom_frame = tk.Frame(top_control_frame, bg="#f4f6f7")
        zoom_frame.pack(side=tk.RIGHT)
        
        self.zoom_out_btn = tk.Button(zoom_frame, text=" 🔍- ", font=("Arial", 10, "bold"), width=3, command=self.zoom_out)
        self.zoom_out_btn.pack(side=tk.LEFT, padx=2)
        
        self.zoom_in_btn = tk.Button(zoom_frame, text=" 🔍+ ", font=("Arial", 10, "bold"), width=3, command=self.zoom_in)
        self.zoom_in_btn.pack(side=tk.LEFT, padx=2)

        mode_card = tk.LabelFrame(inner_pad, text=" Processing Mode & Verify Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        mode_card.pack(fill=tk.X, pady=(0, 12))

        mode_top_frame = tk.Frame(mode_card, bg="#ffffff")
        mode_top_frame.pack(fill=tk.X, pady=5)

        self.mode_lbl = tk.Label(mode_top_frame, text="Current Mode: [ Container-Only Mode ]", bg="#ffffff", fg="#e67e22", font=("Arial", 11, "bold"))
        self.mode_lbl.pack(side=tk.LEFT, padx=5)

        self.mode_toggle_btn = tk.Button(mode_top_frame, text=" 🔀 Switch to Verify Mode ", bg="#2980b9", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=12, pady=5, command=self.toggle_processing_mode)
        self.mode_toggle_btn.pack(side=tk.RIGHT, padx=5)

        today_base_folder = get_today_active_date_folder()
        default_verify_path = os.path.join(today_base_folder, "Main")
        default_container_path = os.path.join(today_base_folder, "Container List")
        default_output_nested = os.path.join(today_base_folder, "BarcodeandStamp")
        default_gdrive_src = today_base_folder

        tk.Label(mode_card, text="Verify Source Location (Main Folder with 'M' filenames):", bg="#ffffff", font=("Arial", self.current_font_size, "bold")).pack(anchor="w", pady=(8, 0))
        verify_src_inner = tk.Frame(mode_card, bg="#ffffff")
        verify_src_inner.pack(fill=tk.X, pady=3)

        self.verify_entry = tk.Entry(verify_src_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.verify_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.verify_entry.insert(0, default_verify_path)
        
        self.btn_verify_src = tk.Button(verify_src_inner, text="Browse...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_verify_source)
        self.btn_verify_src.pack(side=tk.RIGHT)

        self.stats_frame = tk.Frame(inner_pad, bg="#2c3e50", relief=tk.FLAT, bd=0)
        self.stats_frame.pack(fill=tk.X, pady=(0, 15))
        
        stats_inner = tk.Frame(self.stats_frame, bg="#2c3e50")
        stats_inner.pack(pady=10, padx=15, fill=tk.X)
        
        self.status_canvas = tk.Canvas(stats_inner, width=16, height=16, bg="#2c3e50", highlightthickness=0)
        self.status_canvas.pack(side=tk.LEFT, padx=(0, 8))
        self.status_circle = self.status_canvas.create_oval(2, 2, 14, 14, fill="#27ae60", outline="")

        self.stats_lbl = tk.Label(
            stats_inner, 
            text=" 📊 Session Stats — Processed: 0   |   Already Stamped: 0   |   Errors: 0   |   Files Move: 0 ", 
            bg="#2c3e50", 
            fg="white", 
            font=("Arial", 11, "bold")
        )
        self.stats_lbl.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(inner_pad, orient="horizontal", mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 12))

        self.src_card = tk.LabelFrame(inner_pad, text=" Source Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        self.src_card.pack(fill=tk.X, pady=(0, 12))

        tk.Label(self.src_card, text="Source Folder (Container List):", bg="#ffffff", font=("Arial", self.current_font_size, "bold")).pack(anchor="w", pady=(2, 0))
        src_inner = tk.Frame(self.src_card, bg="#ffffff")
        src_inner.pack(fill=tk.X, pady=3)
        
        self.source_entry = tk.Entry(src_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.source_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.source_entry.insert(0, default_container_path)
        
        self.btn_src = tk.Button(src_inner, text="Browse...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_source)
        self.btn_src.pack(side=tk.RIGHT)

        default_stamp_path = self.config_data.get("stamp_path", r"D:\barcodestame\new 2 stamp RB.png")

        self.stamp_card = tk.LabelFrame(inner_pad, text=" Stamp Image Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        self.stamp_card.pack(fill=tk.X, pady=(0, 12))

        self.lbl2 = tk.Label(self.stamp_card, text="Fixed Stamp File Path (.png):", bg="#ffffff", font=("Arial", self.current_font_size, "bold"))
        self.lbl2.pack(anchor="w", pady=(2, 0))
        
        stamp_inner = tk.Frame(self.stamp_card, bg="#ffffff")
        stamp_inner.pack(fill=tk.X, pady=5)
        
        self.fixed_entry = tk.Entry(stamp_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.fixed_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.fixed_entry.insert(0, default_stamp_path)
        
        self.btn_fx = tk.Button(stamp_inner, text="Browse...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_fixed_file)
        self.btn_fx.pack(side=tk.RIGHT)

        self.out_card = tk.LabelFrame(inner_pad, text=" Output Destination Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        self.out_card.pack(fill=tk.X, pady=(0, 12))

        self.lbl3 = tk.Label(self.out_card, text="Output Directory:", bg="#ffffff", font=("Arial", self.current_font_size, "bold"))
        self.lbl3.pack(anchor="w", pady=(2, 0))
        
        out_inner = tk.Frame(self.out_card, bg="#ffffff")
        out_inner.pack(fill=tk.X, pady=5)
        
        self.output_entry = tk.Entry(out_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.output_entry.insert(0, default_output_nested)
        
        self.btn_out = tk.Button(out_inner, text="Browse...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_output)
        self.btn_out.pack(side=tk.RIGHT)

        output_ctrl_frame = tk.Frame(self.out_card, bg="#ffffff")
        output_ctrl_frame.pack(fill=tk.X, pady=(8, 2))
        
        self.auto_create_out_btn = tk.Button(output_ctrl_frame, text=" 📁 Auto-Create Folder ", bg="#27ae60", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=10, pady=6, command=self.auto_create_barcode_stamp_folder)
        self.auto_create_out_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.open_folder_btn = tk.Button(output_ctrl_frame, text=" 📂 Open Output Folder ", bg="#3498db", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=12, pady=6, command=self.open_current_output_folder)
        self.open_folder_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.toggle_list_copy_btn = tk.Button(output_ctrl_frame, text=" 🚀 Start Auto-Copy (list_of_container) ", bg="#8e44ad", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=10, pady=6, command=self.toggle_list_copy_loop)
        self.toggle_list_copy_btn.pack(side=tk.LEFT)

        gdrive_card = tk.LabelFrame(inner_pad, text=" Google Drive OAuth Quota Sync -> Main Customs Docs (Sophal) ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        gdrive_card.pack(fill=tk.X, pady=(0, 12))

        tk.Label(gdrive_card, text="Local Date Folder to Mirror (Updates automatically to today's date):", bg="#ffffff", font=("Arial", self.current_font_size, "bold")).pack(anchor="w", pady=(2, 0))
        gd_src_inner = tk.Frame(gdrive_card, bg="#ffffff")
        gd_src_inner.pack(fill=tk.X, pady=3)
        
        self.gdrive_src_entry = tk.Entry(gd_src_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.gdrive_src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.gdrive_src_entry.insert(0, default_gdrive_src)
        
        self.btn_gd_src = tk.Button(gd_src_inner, text="Browse...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_gdrive_source)
        self.btn_gd_src.pack(side=tk.RIGHT)

        gdrive_ctrl_frame = tk.Frame(gdrive_card, bg="#ffffff")
        gdrive_ctrl_frame.pack(fill=tk.X, pady=(8, 2))

        self.auto_create_gdrive_btn = tk.Button(gdrive_ctrl_frame, text=" 📁 Auto-Create GDrive Folder & Log IDs ", bg="#27ae60", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=10, pady=6, command=self.manual_create_gdrive_folders)
        self.auto_create_gdrive_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.toggle_gdrive_folder_sync_btn = tk.Button(
            gdrive_ctrl_frame, 
            text=" ☁️ Start Auto-Sync CustomsDocs ", 
            bg="#8e44ad", 
            fg="white", 
            font=("Arial", 10, "bold"), 
            relief=tk.FLAT, 
            padx=10, 
            pady=6, 
            command=self.toggle_gdrive_folder_sync_loop
        )
        self.toggle_gdrive_folder_sync_btn.pack(side=tk.LEFT, padx=(0, 8))

        # MANUAL TIME INTERVAL INPUT BOX & START BUTTON FOR AUTO-SYNC CUSTOMSDOCS
        tk.Label(gdrive_ctrl_frame, text="Sync Interval (sec):", bg="#ffffff", font=("Arial", self.current_font_size, "bold")).pack(side=tk.LEFT, padx=(4, 2))
        
        self.sync_interval_entry = tk.Entry(gdrive_ctrl_frame, font=("Arial", self.current_font_size), width=5, relief=tk.SOLID, bd=1)
        self.sync_interval_entry.pack(side=tk.LEFT, padx=(0, 4))
        self.sync_interval_entry.insert(0, "10")

        action_btns_frame = tk.Frame(inner_pad, bg="#f4f6f7")
        action_btns_frame.pack(fill=tk.X, pady=(0, 12))

        self.watch_btn = tk.Button(action_btns_frame, text="Start Auto-Watch & Process", bg="#27ae60", fg="white", font=("Arial", 11, "bold"), bd=0, relief=tk.FLAT, command=self.toggle_watch)
        self.watch_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5), ipady=8)

        self.auto_sequence_btn = tk.Button(action_btns_frame, text="Run Full 5-Step Automation", bg="#2980b9", fg="white", font=("Arial", 11, "bold"), bd=0, relief=tk.FLAT, command=lambda: threading.Thread(target=self.run_full_automated_sequence, daemon=True).start())
        self.auto_sequence_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 5), ipady=8)

        self.process_copy_btn = tk.Button(action_btns_frame, text="Start Auto-Copy & Process (Keep Source)", bg="#d35400", fg="white", font=("Arial", 11, "bold"), bd=0, relief=tk.FLAT, command=self.toggle_copy_processing)
        self.process_copy_btn.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(5, 0), ipady=8)

        self.log_box = scrolledtext.ScrolledText(inner_pad, font=("Consolas", self.current_font_size), height=10, state="normal", relief=tk.SOLID, bd=1)
        self.log_box.pack(fill=tk.BOTH, expand=True)

        self.update_output_file_count()

    def get_oauth_headers(self):
        creds = None
        token_path = "token.json"
        client_secrets_path = r"D:\barcodestame\credentials.json"
        
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
        return {"Authorization": f"Bearer {creds.token}"}

    def save_current_paths(self):
        cfg = {
            "verify_path": self.verify_entry.get().strip(),
            "source_path": self.source_entry.get().strip(),
            "stamp_path": self.fixed_entry.get().strip(),
            "output_path": self.output_entry.get().strip(),
            "gdrive_src": self.gdrive_src_entry.get().strip()
        }
        save_config(cfg)

    def select_source(self):
        folder = filedialog.askdirectory()
        if folder:
            self.source_entry.delete(0, tk.END)
            self.source_entry.insert(0, folder)
            self.save_current_paths()

    def select_verify_source(self):
        folder = filedialog.askdirectory()
        if folder:
            self.verify_entry.delete(0, tk.END)
            self.verify_entry.insert(0, folder)
            self.save_current_paths()

    def select_gdrive_source(self):
        folder = filedialog.askdirectory()
        if folder:
            self.gdrive_src_entry.delete(0, tk.END)
            self.gdrive_src_entry.insert(0, folder)
            self.save_current_paths()

    def select_fixed_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png;*.jpg;*.jpeg")])
        if file_path:
            self.fixed_entry.delete(0, tk.END)
            self.fixed_entry.insert(0, file_path)
            self.save_current_paths()

    def select_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, folder)
            self.save_current_paths()

    def update_output_file_count(self):
        out_dir = self.output_entry.get().strip()
        if out_dir and os.path.exists(out_dir):
            try:
                files_list = [f for f in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, f))]
                self.count_files_move = len(files_list)
            except Exception:
                self.count_files_move = 0
        else:
            self.count_files_move = 0
        self.update_stats_display()

    def open_current_output_folder(self):
        target_dir = self.output_entry.get()
        if target_dir and os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)
            subprocess.Popen(f'explorer "{os.path.abspath(target_dir)}"')
        else:
            messagebox.showinfo("Folder Notice", "The output folder path does not exist yet.")

    def toggle_processing_mode(self):
        if not self.verify_mode_active:
            self.toggle_processing_mode_silent()
            self.verify_mode_active = True
            self.mode_lbl.config(text="Current Mode: [ Verify Mode (Main 'M' Register Match Active) ]", fg="#2ecc71")
            self.mode_toggle_btn.config(text=" 🔀 Switch to Container-Only Mode ", bg="#c0392b")
            self.log_box.insert(tk.END, f"Verify Mode activated. Loaded {len(self.register_numbers_set)} register numbers from Main folder files containing 'M'.\n")
            self.log_box.see(tk.END)
        else:
            self.verify_mode_active = False
            self.register_numbers_set.clear()
            self.mode_lbl.config(text="Current Mode: [ Container-Only Mode ]", fg="#e67e22")
            self.mode_toggle_btn.config(text=" 🔀 Switch to Verify Mode ", bg="#2980b9")
            self.log_box.insert(tk.END, "Switched back to Container-Only Mode.\n")
            self.log_box.see(tk.END)

    def toggle_processing_mode_silent(self):
        verify_path = self.verify_entry.get().strip()
        loaded_regs = {} 
        try:
            if verify_path and os.path.exists(verify_path):
                if os.path.isdir(verify_path):
                    for root_dir, _, files in os.walk(verify_path):
                        for file in files:
                            if 'M' in file.upper():
                                match_e = re.search(r'E\s*(\d+)', file, re.IGNORECASE)
                                if match_e:
                                    reg_num = match_e.group(1)
                                    loaded_regs[reg_num] = file
                elif os.path.isfile(verify_path):
                    if 'M' in verify_path.upper():
                        match_e = re.search(r'E\s*(\d+)', verify_path, re.IGNORECASE)
                        if match_e:
                            reg_num = match_e.group(1)
                            loaded_regs[reg_num] = os.path.basename(verify_path)
            self.register_numbers_set = loaded_regs
        except Exception:
            pass

    def toggle_list_copy_loop(self):
        if not self.is_list_copy_looping:
            self.is_list_copy_looping = True
            self.toggle_list_copy_btn.config(text=" ⏹️ Stop Auto-Copy (list_of_container) ", bg="#c0392b")
            self.log_box.insert(tk.END, "Auto-Copy loop for 'list_of_container' started...\n")
            self.log_box.see(tk.END)
            threading.Thread(target=self.list_copy_loop_worker, daemon=True).start()
        else:
            self.is_list_copy_looping = False
            self.toggle_list_copy_btn.config(text=" 🚀 Start Auto-Copy (list_of_container) ", bg="#8e44ad")
            self.log_box.insert(tk.END, "Auto-Copy loop for 'list_of_container' stopped.\n")
            self.log_box.see(tk.END)

    def list_copy_loop_worker(self):
        while self.is_list_copy_looping:
            try:
                source_dir = self.source_entry.get().strip()
                output_dir = self.output_entry.get().strip()
                
                if source_dir and output_dir and os.path.exists(source_dir):
                    target_list_dir = os.path.join(output_dir, "list_of_container")
                    os.makedirs(target_list_dir, exist_ok=True)
                    files = os.listdir(source_dir)
                    for file_name in files:
                        if not self.is_list_copy_looping:
                            break
                        src_file_path = os.path.join(source_dir, file_name)
                        dest_file_path = os.path.join(target_list_dir, file_name)
                        
                        if os.path.isfile(src_file_path):
                            if is_file_ready(src_file_path):
                                try:
                                    shutil.copy2(src_file_path, dest_file_path)
                                except Exception:
                                    pass
            except Exception:
                pass
            time.sleep(0.5)

    def get_or_create_folder_id(self, headers, parent_id, folder_name):
        url = "https://www.googleapis.com/drive/v3/files"
        params = {
            "q": f"'{parent_id}' in parents and name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            "pageSize": 1,
            "fields": "files(id, name)",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True
        }
        res = requests.get(url, headers=headers, params=params)
        if res.status_code == 200:
            files = res.json().get("files", [])
            if files:
                return files[0]["id"]
        
        meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
        r_create = requests.post(url, headers=headers, json=meta, params={"supportsAllDrives": True})
        if r_create.status_code == 200:
            return r_create.json().get("id")
        else:
            err_str = f"API Error creating '{folder_name}' ({r_create.status_code}): {r_create.text}"
            log_error_to_file(err_str)
        return None

    def log_folder_structure_to_sheet(self, date_label, sub_main_name, sub_main_id, subfolder_ids_map):
        token_path = "token.json"
        if not os.path.exists(token_path):
            return
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            client = gspread.authorize(creds)
            spreadsheet = client.open_by_key("1EDogJjSd9qJd8Y8oUgPtkBPH2kKBbABRDm3raem-eUY")
            sheet = spreadsheet.get_worksheet(0)
            
            main_id = subfolder_ids_map.get("Main", "")
            part_id = subfolder_ids_map.get("Part", "")
            container_list_id = subfolder_ids_map.get("Container List", "")
            barcode_stamp_id = subfolder_ids_map.get("BarCodeAndStamp", "")
            report_id = subfolder_ids_map.get("Report", "")
            vgm_id = subfolder_ids_map.get("Container Match Format (VGM)", "")

            row_data = [
                date_label, 
                sub_main_name, 
                sub_main_id, 
                main_id, 
                part_id, 
                container_list_id, 
                vgm_id, 
                barcode_stamp_id, 
                report_id
            ]

            try:
                cell = sheet.find(sub_main_name)
                if cell:
                    sheet.update(range_name=f'A{cell.row}:I{cell.row}', values=[row_data])
                else:
                    sheet.append_row(row_data)
            except gspread.exceptions.CellNotFound:
                sheet.append_row(row_data)
        except Exception as e:
            log_error_to_file(f"Error logging folder IDs to Google Sheet: {e}")

    def manual_create_gdrive_folders(self):
        threading.Thread(target=self.create_gdrive_folders_worker, daemon=True).start()

    def create_gdrive_folders_worker(self):
        target_path = self.gdrive_src_entry.get().strip()
        parent_id = TARGET_PARENT_FOLDER_ID

        try:
            headers = self.get_oauth_headers()
            folder_base_name = os.path.basename(os.path.normpath(target_path))
            date_match = re.search(r'(\d{2}-\w{3}-\d{4})', folder_base_name)
            date_str = date_match.group(1) if date_match else datetime.datetime.now().strftime("%d-%b-%Y")

            c_name = f"CustomsDocs_{date_str}"
            subfolders_to_create = [
                "BarCodeAndStamp",
                "Container List",
                "Container Match Format (VGM)",
                "Main",
                "Part",
                "Report"
            ]
            
            gdrive_folder_id = self.get_or_create_folder_id(headers, parent_id, c_name)
            if gdrive_folder_id:
                subfolder_id_map = {}
                for sub in subfolders_to_create:
                    sub_id = self.get_or_create_folder_id(headers, gdrive_folder_id, sub)
                    if sub_id:
                        subfolder_id_map[sub] = sub_id

                self.log_folder_structure_to_sheet(date_str, c_name, gdrive_folder_id, subfolder_id_map)
                self.sync_folder_by_id(target_path, gdrive_folder_id, subfolder_id_map, headers)
            else:
                log_error_to_file(f"-> ERROR: Could not create folder {c_name}")
        except Exception as e:
            log_error_to_file(f"Failed to create GDrive folder:\n{e}")

    def toggle_gdrive_folder_sync_loop(self):
        if not self.is_gdrive_folder_sync_looping:
            self.is_gdrive_folder_sync_looping = True
            self.toggle_gdrive_folder_sync_btn.config(text=" ⏹️ Stop Auto-Sync CustomsDocs ", bg="#c0392b")
            self.log_box.insert(tk.END, "Auto-Sync CustomsDocs loop started with manual time interval...\n")
            self.log_box.see(tk.END)
            threading.Thread(target=self.gdrive_folder_sync_loop_worker, daemon=True).start()
        else:
            self.is_gdrive_folder_sync_looping = False
            self.toggle_gdrive_folder_sync_btn.config(text=" ☁️ Start Auto-Sync CustomsDocs ", bg="#8e44ad")
            self.log_box.insert(tk.END, "Auto-Sync CustomsDocs loop stopped.\n")
            self.log_box.see(tk.END)

    def gdrive_folder_sync_loop_worker(self):
        while self.is_gdrive_folder_sync_looping:
            try:
                try:
                    interval = float(self.sync_interval_entry.get().strip())
                    if interval <= 0:
                        interval = 10.0
                except:
                    interval = 10.0

                target_path = self.gdrive_src_entry.get().strip()
                parent_id = TARGET_PARENT_FOLDER_ID
                
                if not os.path.exists(target_path):
                    time.sleep(5)
                    continue

                headers = self.get_oauth_headers()
                folder_base_name = os.path.basename(os.path.normpath(target_path))
                date_match = re.search(r'(\d{2}-\w{3}-\d{4})', folder_base_name)
                date_str = date_match.group(1) if date_match else datetime.datetime.now().strftime("%d-%b-%Y")

                c_name = f"CustomsDocs_{date_str}"
                gdrive_folder_id = self.get_or_create_folder_id(headers, parent_id, c_name)
                if gdrive_folder_id:
                    subfolder_id_map = {}
                    for sub in ["BarCodeAndStamp", "Container List", "Container Match Format (VGM)", "Main", "Part", "Report"]:
                        if not self.is_gdrive_folder_sync_looping:
                            break
                        sub_id = self.get_or_create_folder_id(headers, gdrive_folder_id, sub)
                        if sub_id:
                            subfolder_ids_map[sub] = sub_id

                    self.log_folder_structure_to_sheet(date_str, c_name, gdrive_folder_id, subfolder_ids_map)
                    self.sync_folder_by_id(target_path, gdrive_folder_id, subfolder_ids_map, headers)
                
                elapsed = 0.0
                while elapsed < interval and self.is_gdrive_folder_sync_looping:
                    time.sleep(0.5)
                    elapsed += 0.5
            except Exception as e:
                log_error_to_file(f"GDrive Sync Loop Error: {e}")
                time.sleep(2)

    def sync_folder_by_id(self, local_dir, root_gdrive_id, subfolder_id_map, headers):
        try:
            folder_name = os.path.basename(os.path.normpath(local_dir)).lower()
            
            # STRICT TARGETING: ONLY Report folder and Container List check continuously. 
            # All other folders (Main, Part, BarCodeAndStamp, etc.) skip existing files instantly for maximum speed!
            is_report_or_list_folder = ("report" in folder_name) or ("container list" in folder_name)

            existing_gdrive_files = {}
            try:
                page_token = None
                while True:
                    list_url = "https://www.googleapis.com/drive/v3/files"
                    params = {
                        "q": f"'{root_gdrive_id}' in parents and trashed = false",
                        "fields": "nextPageToken, files(id, name)",
                        "supportsAllDrives": True,
                        "includeItemsFromAllDrives": True,
                        "pageSize": 1000
                    }
                    if page_token:
                        params["pageToken"] = page_token

                    res = requests.get(list_url, headers=headers, params=params)
                    if res.status_code == 200:
                        data = res.json()
                        for f in data.get("files", []):
                            existing_gdrive_files[f["name"].strip().lower()] = f.get("id")
                        page_token = data.get("nextPageToken")
                        if not page_token:
                            break
                    else:
                        break
            except Exception:
                pass

            if not os.path.exists(local_dir):
                return
            items = os.listdir(local_dir)
            
            def upload_single_item(item):
                local_item_path = os.path.join(local_dir, item)
                target_pid = subfolder_id_map.get(item, root_gdrive_id)
                
                if os.path.isdir(local_item_path):
                    sub_id = subfolder_id_map.get(item)
                    if not sub_id:
                        sub_id = self.get_or_create_folder_id(headers, root_gdrive_id, item)
                    
                    if sub_id:
                        sub_map = {item: sub_id}
                        self.sync_folder_by_id(local_item_path, sub_id, sub_map, headers)
                
                elif os.path.isfile(local_item_path):
                    clean_item = item.strip().lower()

                    try:
                        file_id = existing_gdrive_files.get(clean_item)

                        # FAST SPEED OPTIMIZATION: If it's an image/PDF folder (Main, Part, etc.) and already exists on Google Drive, skip instantly!
                        if not is_report_or_list_folder and file_id:
                            return

                        read_path = local_item_path
                        temp_shadow_path = None
                        if is_report_or_list_folder:
                            try:
                                temp_shadow_path = local_item_path + ".tmp_sync_force"
                                shutil.copy2(local_item_path, temp_shadow_path)
                                read_path = temp_shadow_path
                            except Exception:
                                read_path = local_item_path

                        file_content = b""
                        for _ in range(5):
                            try:
                                with open(read_path, 'rb') as f:
                                    file_content = f.read()
                                if file_content:
                                    break
                            except Exception:
                                time.sleep(0.2)

                        if temp_shadow_path and os.path.exists(temp_shadow_path):
                            try:
                                os.remove(temp_shadow_path)
                            except:
                                pass

                        if not file_content:
                            return

                        boundary = 'foo_bar_baz'
                        headers_mp = {"Authorization": headers["Authorization"], "Content-Type": f"multipart/related; boundary={boundary}"}
                        metadata_part = json.dumps({"name": item, "parents": [target_pid]})
                        body = (
                            f"--{boundary}\r\n"
                            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                            f"{metadata_part}\r\n"
                            f"--{boundary}\r\n"
                            f"Content-Type: application/octet-stream\r\n\r\n"
                        ).encode('utf-8') + file_content + f"\r\n--{boundary}--".encode('utf-8')
                        
                        if file_id:
                            update_url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=multipart&supportsAllDrives=true"
                            for attempt in range(3):
                                upload_res = requests.patch(update_url, headers=headers_mp, data=body)
                                if upload_res.status_code == 200:
                                    break
                                elif upload_res.status_code == 403:
                                    time.sleep(2.0 * (attempt + 1))
                                else:
                                    break
                        else:
                            create_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true"
                            for attempt in range(3):
                                upload_res = requests.post(create_url, headers=headers_mp, data=body)
                                if upload_res.status_code == 200:
                                    existing_gdrive_files[clean_item] = upload_res.json().get("id")
                                    break
                                elif upload_res.status_code == 403:
                                    time.sleep(2.0 * (attempt + 1))
                                else:
                                    break
                        time.sleep(0.05)
                    except Exception as e:
                        log_error_to_file(f"Upload/Update exception on {item}: {e}")

            worker_count = 1 if is_report_or_list_folder else 6
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                executor.map(upload_single_item, items)

        except Exception as e:
            log_error_to_file(f"Sync error on {local_dir}: {e}")

    def update_stats_display(self):
        stats_text = f" 📊 Session Stats — Processed: {self.count_processed}   |   Already Stamped: {self.count_already_stamped}   |   Errors: {self.count_errors}   |   Files Move: {self.count_files_move} "
        self.stats_lbl.config(text=stats_text)

    def jump_to_error_log(self):
        log_content = self.log_box.get("1.0", tk.END)
        lines = log_content.split('\n')
        self.log_box.tag_remove("highlight", "1.0", tk.END)
        for idx, line in enumerate(lines):
            if "Error" in line:
                line_number = idx + 1
                self.log_box.see(f"{line_number}.0")
                self.log_box.tag_add("highlight", f"{line_number}.0", f"{line_number}.end")
                self.log_box.tag_config("highlight", background="yellow", foreground="black")
                return
        self.log_box.insert(tk.END, "No errors found in current log.\n")
        self.log_box.see(tk.END)

    def toggle_expand_log(self):
        self.is_log_expanded = not self.is_log_expanded
        if self.is_log_expanded:
            self.src_card.pack_forget()
            self.stamp_card.pack_forget()
            self.out_card.pack_forget()
            self.expand_log_btn.config(text=" 📜 Restore View ", bg="#7f8c8d")
            self.log_box.config(height=32)
        else:
            self.src_card.pack(fill=tk.X, pady=(0, 12))
            self.stamp_card.pack(fill=tk.X, pady=(0, 12))
            self.out_card.pack(fill=tk.X, pady=(0, 12))
            self.expand_log_btn.config(text=" 📜 Expand Log ", bg="#34495e")
            self.log_box.config(height=10)

    def auto_create_barcode_stamp_folder(self, silent=True):
        target_dir = self.output_entry.get().strip()
        if target_dir:
            try:
                os.makedirs(target_dir, exist_ok=True)
                os.makedirs(os.path.join(target_dir, "Error_Files"), exist_ok=True)
                if not silent:
                    messagebox.showinfo("Success", f"Output folder created successfully:\n{target_dir}")
            except Exception as e:
                log_error_to_file(f"Folder creation error: {e}")
                if not silent:
                    messagebox.showerror("Error", f"Could not create folder:\n{e}")

    def reset_history(self):
        self.count_processed = 0
        self.count_already_stamped = 0
        self.count_errors = 0
        self.seen_containers.clear()
        self.first_seen_files.clear()
        self.processed_source_files.clear()
        self.progress_bar["value"] = 0
        self.update_output_file_count()
        self.log_box.delete("1.0", tk.END)
        self.log_box.insert(tk.END, "Log cleared and stats reset.\n")
        self.log_box.see(tk.END)

    def load_stamp_image(self):
        fixed_file = self.fixed_entry.get()
        if fixed_file and os.path.exists(fixed_file):
            try:
                img_raw = Image.open(fixed_file).convert("RGBA")
                datas = img_raw.getdata()
                new_data = []
                for item in datas:
                    if (item[0] > 200 and item[1] > 200 and item[2] > 200) or (abs(item[0] - item[1]) < 10 and abs(item[1] - item[2]) < 10 and item[0] > 150):
                        new_data.append((255, 255, 255, 0))
                    else:
                        new_data.append(item)
                img_raw.putdata(new_data)
                enhancer = ImageEnhance.Color(img_raw)
                img_raw = enhancer.enhance(3.5)
                contrast_enhancer = ImageEnhance.Contrast(img_raw)
                img_raw = contrast_enhancer.enhance(3.0)
                img_raw.thumbnail((500, 500))
                return img_raw
            except Exception as e:
                log_error_to_file(f"Stamp image load error: {e}")
        return None

    def toggle_watch(self):
        if not self.is_watching:
            target_output_dir = self.output_entry.get().strip()
            source = self.source_entry.get().strip()
            if not source or not target_output_dir:
                messagebox.showerror("Missing Information", "Please specify Source folder and Output directory.")
                return
            
            os.makedirs(target_output_dir, exist_ok=True)
            os.makedirs(os.path.join(target_output_dir, "Error_Files"), exist_ok=True)
            self.session_output_dir = target_output_dir
            
            self.is_watching = True
            self.status_canvas.itemconfig(self.status_circle, fill="#e74c3c")
            self.watch_btn.config(text="Stop Auto-Watch Mode", bg="#c0392b")
            self.log_box.insert(tk.END, f"Auto-Watch started. Output: {self.session_output_dir}\n")
            self.log_box.see(tk.END)
            
            self.toggle_processing_mode_silent()
            threading.Thread(target=self.watch_folder_loop, daemon=True).start()
        else:
            self.is_watching = False
            self.status_canvas.itemconfig(self.status_circle, fill="#27ae60")
            self.watch_btn.config(text="Start Auto-Watch & Process", bg="#27ae60")
            self.log_box.insert(tk.END, f"Auto-Watch mode stopped.\n")
            self.log_box.see(tk.END)

    def watch_folder_loop(self):
        source_dir = self.source_entry.get()
        fixed_img = self.load_stamp_image()

        while self.is_watching:
            try:
                self.toggle_processing_mode_silent()
                if os.path.exists(source_dir):
                    os.makedirs(self.session_output_dir, exist_ok=True)
                    all_files = [f for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))]
                    
                    valid_files = [f for f in all_files if f not in self.processed_source_files and "@" in f]
                    other_files = [f for f in all_files if f not in self.processed_source_files and "@" not in f]
                    container_files = valid_files + other_files
                    
                    if container_files:
                        total_files_batch = len(container_files)
                        self.progress_bar["maximum"] = total_files_batch
                        self.progress_bar["value"] = 0

                        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                            futures = {
                                executor.submit(process_single_file, file_name, source_dir, self.session_output_dir, fixed_img, copy_only=False, seen_containers=self.seen_containers, first_seen_files=self.first_seen_files, verify_mode=self.verify_mode_active, register_numbers=self.register_numbers_set): file_name 
                                for file_name in container_files
                            }
                            completed_count = 0
                            for future in concurrent.futures.as_completed(futures):
                                file_name = futures[future]
                                self.processed_source_files.add(file_name)
                                res_status, result_msg = future.result()
                                if res_status in ["processed", "duplicate_container"]:
                                    self.count_processed += 1
                                    if res_status == "processed":
                                        self.count_already_stamped += 1
                                elif res_status == "error":
                                    self.count_errors += 1
                                if result_msg:
                                    self.log_box.insert(tk.END, result_msg + "\n")
                                    self.log_box.see(tk.END)
                                completed_count += 1
                                self.progress_bar["value"] = completed_count
                                self.update_output_file_count()
            except Exception as e:
                log_error_to_file(f"Watch folder loop error: {e}")
            time.sleep(0.5)

    def toggle_copy_processing(self):
        if not self.is_copy_processing:
            target_output_dir = self.output_entry.get().strip()
            source = self.source_entry.get().strip()
            if not source or not target_output_dir:
                messagebox.showerror("Error", "Please specify Source folder and Output directory.")
                return

            os.makedirs(target_output_dir, exist_ok=True)
            self.session_output_dir = target_output_dir
            self.is_copy_processing = True
            self.process_copy_btn.config(text="Stop Auto-Copy & Process", bg="#c0392b")
            self.log_box.insert(tk.END, f"Auto-Copy & Process started (Keep Source). Output: {target_output_dir}\n")
            self.log_box.see(tk.END)

            self.toggle_processing_mode_silent()
            threading.Thread(target=self.copy_processing_loop_worker, daemon=True).start()
        else:
            self.is_copy_processing = False
            self.process_copy_btn.config(text="Start Auto-Copy & Process (Keep Source)", bg="#d35400")
            self.log_box.insert(tk.END, f"Auto-Copy & Process stopped.\n")
            self.log_box.see(tk.END)

    def copy_processing_loop_worker(self):
        source_dir = self.source_entry.get()
        fixed_img = self.load_stamp_image()

        while self.is_copy_processing:
            try:
                self.toggle_processing_mode_silent()
                if os.path.exists(source_dir):
                    os.makedirs(self.session_output_dir, exist_ok=True)
                    all_files = [f for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))]
                    
                    valid_files = [f for f in all_files if f not in self.processed_source_files and "@" in f]
                    other_files = [f for f in all_files if f not in self.processed_source_files and "@" not in f]
                    container_files = valid_files + other_files
                    
                    if container_files:
                        total_files_batch = len(container_files)
                        self.progress_bar["maximum"] = total_files_batch
                        self.progress_bar["value"] = 0

                        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                            futures = {
                                executor.submit(process_single_file, file_name, source_dir, self.session_output_dir, fixed_img, copy_only=True, seen_containers=self.seen_containers, first_seen_files=self.first_seen_files, verify_mode=self.verify_mode_active, register_numbers=self.register_numbers_set): file_name 
                                for file_name in container_files
                            }
                            completed_count = 0
                            for future in concurrent.futures.as_completed(futures):
                                file_name = futures[future]
                                self.processed_source_files.add(file_name)
                                res_status, result_msg = future.result()
                                if res_status in ["processed", "duplicate_container"]:
                                    self.count_processed += 1
                                    if res_status == "processed":
                                        self.count_already_stamped += 1
                                elif res_status == "error":
                                    self.count_errors += 1
                                if result_msg:
                                    self.log_box.insert(tk.END, result_msg + "\n")
                                    self.log_box.see(tk.END)
                                completed_count += 1
                                self.progress_bar["value"] = completed_count
                                self.update_output_file_count()
            except Exception as e:
                log_error_to_file(f"Copy loop error: {e}")
            time.sleep(0.5)

    def zoom_in(self):
        if self.current_font_size < 22:
            self.current_font_size += 2
            self.update_font_sizes()

    def zoom_out(self):
        if self.current_font_size > 8:
            self.current_font_size -= 2
            self.update_font_sizes()

    def update_font_sizes(self):
        f_norm = ("Arial", self.current_font_size)
        f_log = ("Consolas", self.current_font_size)
        for ent in [self.source_entry, self.fixed_entry, self.output_entry, self.verify_entry, self.gdrive_src_entry]:
            ent.config(font=f_norm)
        for btn in [self.btn_src, self.btn_fx, self.btn_out, self.btn_verify_src, self.btn_gd_src]:
            btn.config(font=f_norm)
        self.log_box.config(font=f_log)

def run_fully_automatic_startup(app_instance):
    app_instance.run_full_automated_sequence()
    
    def trigger_loops():
        if not app_instance.is_watching:
            target_output_dir = app_instance.output_entry.get().strip()
            source = app_instance.source_entry.get().strip()
            if source and target_output_dir:
                os.makedirs(target_output_dir, exist_ok=True)
                os.makedirs(os.path.join(target_output_dir, "Error_Files"), exist_ok=True)
                app_instance.session_output_dir = target_output_dir
                app_instance.is_watching = True
                app_instance.status_canvas.itemconfig(app_instance.status_circle, fill="#e74c3c")
                app_instance.watch_btn.config(text="Stop Auto-Watch Mode", bg="#c0392b")
                app_instance.log_box.insert(tk.END, f"Auto-Watch started. Output: {app_instance.session_output_dir}\n")
                app_instance.log_box.see(tk.END)
                app_instance.toggle_processing_mode_silent()
                threading.Thread(target=app_instance.watch_folder_loop, daemon=True).start()

        if not app_instance.is_list_copy_looping:
            app_instance.is_list_copy_looping = True
            app_instance.toggle_list_copy_btn.config(text=" ⏹️ Stop Auto-Copy (list_of_container) ", bg="#c0392b")
            app_instance.log_box.insert(tk.END, "Auto-Copy loop for 'list_of_container' started...\n")
            app_instance.log_box.see(tk.END)
            threading.Thread(target=app_instance.list_copy_loop_worker, daemon=True).start()

        if not app_instance.is_gdrive_folder_sync_looping:
            app_instance.is_gdrive_folder_sync_looping = True
            app_instance.toggle_gdrive_folder_sync_btn.config(text=" ⏹️ Stop Auto-Sync CustomsDocs ", bg="#c0392b")
            app_instance.log_box.insert(tk.END, "Auto-Sync CustomsDocs loop started...\n")
            app_instance.log_box.see(tk.END)
            threading.Thread(target=app_instance.gdrive_folder_sync_loop_worker, daemon=True).start()

    app_instance.root.after(100, trigger_loops)

if __name__ == "__main__":
    root = tk.Tk()
    app = BarcodeApp(root)
    threading.Thread(target=run_fully_automatic_startup, args=(app,), daemon=True).start()
    root.mainloop()