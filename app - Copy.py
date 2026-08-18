import os
import re
import datetime
import csv
import shutil
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox, simpledialog
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageEnhance
import requests
from io import BytesIO
import concurrent.futures
import time
import threading
import subprocess
import gspread

duplicate_lock = threading.Lock()

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

def process_single_file(file_name, source_dir, output_dir, fixed_img, copy_only=False, seen_containers=None, first_seen_files=None, verify_mode=False, register_numbers=None):
    source_file_path = os.path.join(source_dir, file_name)
    output_file_path = os.path.join(output_dir, file_name)
    
    if not file_name.lower().endswith('.jpg'):
        return ("ignored", None)

    # Silent file lock waiter
    max_lock_retries = 10
    for _ in range(max_lock_retries):
        try:
            if os.path.exists(source_file_path):
                with open(source_file_path, 'rb'):
                    pass
            break
        except (PermissionError, IOError):
            time.sleep(0.3)
    else:
        return ("ignored", None)

    try:
        if "@" not in file_name:
            return ("error", f"Error with {file_name}: Missing '@' separator")

        before_at, after_at = file_name.split("@", 1)
        number_matches = re.findall(r'\d+', after_at)
        
        if not number_matches:
            return ("error", f"Error with {file_name}: No numbers found after '@'")

        barcode_text = number_matches[0]

        # --- VERIFY MODE CHECK (Matching Register Number + Letter M) ---
        if verify_mode and register_numbers is not None and len(register_numbers) > 0:
            name_without_ext = os.path.splitext(file_name)[0].strip()
            has_letter_m = name_without_ext.upper().endswith('M')

            matched_reg = False
            for num in number_matches:
                if num in register_numbers or barcode_text in register_numbers:
                    matched_reg = True
                    break
            
            if not matched_reg or not has_letter_m:
                mismatch_folder = os.path.join(output_dir, "Unverified_Registers")
                os.makedirs(mismatch_folder, exist_ok=True)
                mismatch_path = os.path.join(mismatch_folder, file_name)
                if copy_only:
                    shutil.copy2(source_file_path, mismatch_path)
                else:
                    shutil.move(source_file_path, mismatch_path)
                return ("unverified", f"Skipped (Verify Failed - Register/Letter M Mismatch): Moved to Unverified_Registers | {file_name}")
        
        # --- THREAD-SAFE DUPLICATE CONTAINER NUMBER FILTER & REPORTING ---
        if seen_containers is not None and first_seen_files is not None:
            with duplicate_lock:
                is_duplicate = barcode_text in seen_containers

                if is_duplicate:
                    dup_folder = os.path.join(output_dir, "Duplicate_containers")
                    os.makedirs(dup_folder, exist_ok=True)
                    dup_file_path = os.path.join(dup_folder, file_name)
                    report_path = os.path.join(dup_folder, "duplicates_report.csv")
                    
                    try:
                        file_exists = os.path.exists(report_path)
                        with open(report_path, mode='a', newline='', encoding='utf-8') as f:
                            writer = csv.writer(f)
                            if not file_exists: 
                                writer.writerow(["Timestamp", "Duplicate File", "Container Number", "Original File"])
                            writer.writerow([
                                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                                file_name, 
                                barcode_text, 
                                first_seen_files.get(barcode_text, "Unknown")
                            ])
                    except PermissionError:
                        pass 
                    
                    if copy_only:
                        shutil.copy2(source_file_path, dup_file_path)
                    else:
                        shutil.move(source_file_path, dup_file_path)
                    
                    return ("duplicate_container", f"Skipped Duplicate Container ({barcode_text}): Moved to Duplicate_containers | {file_name}")
                else:
                    seen_containers.add(barcode_text)
                    if barcode_text not in first_seen_files:
                        first_seen_files[barcode_text] = file_name

        # --- CHECK IF OUTPUT FILE ALREADY EXISTS ---
        if os.path.exists(output_file_path):
            if not copy_only and os.path.exists(source_file_path):
                try:
                    os.remove(source_file_path)
                except:
                    pass
            return ("output_existing", None)

        # --- STAMPING & BARCODE LOGIC ---
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
            return ("error", f"Error with {file_name}: API error code {response.status_code}")

        barcode_img = Image.open(BytesIO(response.content)).convert("RGBA")
        barcode_img = barcode_img.resize((1000, 250))
        
        gray_barcode = ImageOps.grayscale(barcode_img)
        green_barcode = Image.new("RGBA", barcode_img.size, (0, 150, 0, 255))
        barcode_img = Image.composite(green_barcode, Image.new("RGBA", barcode_img.size, (255, 255, 255, 0)), ImageOps.invert(gray_barcode))

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
            filename_lines = draw_wrapped_text(draw, file_name, font, max_text_width)
            
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
                os.remove(source_file_path)
            except:
                pass
        
        action_label = "Copied & Processed" if copy_only else "Success & Moved"
        return ("processed", f"{action_label}: {file_name} | Barcode: {barcode_text}")
        
    except Exception as e:
        return ("error", f"Error with {file_name}: {e}")

class BarcodeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("High-Speed Move-Processing Barcode App")
        self.root.geometry("980x1850")
        self.root.configure(bg="#f4f6f7")
        
        self.is_watching = False
        self.is_copy_processing = False  
        self.is_sheet_sync_looping = False  
        self.is_list_copy_looping = False  
        self.current_font_size = 11  
        self.session_output_dir = "" 
        self.is_log_expanded = False
        
        self.verify_mode_active = False 
        self.register_numbers_set = set() 
        
        self.target_sheets = ["report"]
        self.seen_containers = set()
        self.first_seen_files = {}  
        self.processed_source_files = set()
        
        self.count_processed = 0
        self.count_already_stamped = 0
        self.count_files_move = 0
        self.count_errors = 0
        
        self.main_frame = tk.Frame(root, bg="#f4f6f7")
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        
        top_control_frame = tk.Frame(self.main_frame, bg="#f4f6f7")
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

        # --- MODE SWITCH & VERIFY CONFIG BAR ---
        mode_card = tk.LabelFrame(self.main_frame, text=" Processing Mode & Verify Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        mode_card.pack(fill=tk.X, pady=(0, 12))

        mode_top_frame = tk.Frame(mode_card, bg="#ffffff")
        mode_top_frame.pack(fill=tk.X, pady=5)

        self.mode_lbl = tk.Label(mode_top_frame, text="Current Mode: [ Container-Only Mode ]", bg="#ffffff", fg="#e67e22", font=("Arial", 11, "bold"))
        self.mode_lbl.pack(side=tk.LEFT, padx=5)

        self.mode_toggle_btn = tk.Button(mode_top_frame, text=" 🔀 Switch to Verify Mode ", bg="#2980b9", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=12, pady=5, command=self.toggle_processing_mode)
        self.mode_toggle_btn.pack(side=tk.RIGHT, padx=5)

        tk.Label(mode_card, text="Verify Source Location (Register Folder / Images / CSV):", bg="#ffffff", font=("Arial", self.current_font_size, "bold")).pack(anchor="w", pady=(8, 0))
        verify_src_inner = tk.Frame(mode_card, bg="#ffffff")
        verify_src_inner.pack(fill=tk.X, pady=3)

        today_date_str = datetime.datetime.now().strftime("%d-%b-%Y")
        default_verify_path = rf"D:\Scan\Main OutputFastOCR\OutputFastOCR_{today_date_str}\Main"

        self.verify_entry = tk.Entry(verify_src_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.verify_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.verify_entry.insert(0, default_verify_path)
        
        self.btn_verify_src = tk.Button(verify_src_inner, text="Browse...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_verify_source)
        self.btn_verify_src.pack(side=tk.RIGHT)

        # --- STATS HEADER ---
        self.stats_frame = tk.Frame(self.main_frame, bg="#2c3e50", relief=tk.FLAT, bd=0)
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

        # --- SOURCE FOLDER ---
        default_container_path = rf"D:\Scan\Main OutputFastOCR\OutputFastOCR_{today_date_str}\Container List"

        self.src_card = tk.LabelFrame(self.main_frame, text=" Source Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        self.src_card.pack(fill=tk.X, pady=(0, 12))

        tk.Label(self.src_card, text="Source Folder (Container List):", bg="#ffffff", font=("Arial", self.current_font_size, "bold")).pack(anchor="w", pady=(2, 0))
        src_inner = tk.Frame(self.src_card, bg="#ffffff")
        src_inner.pack(fill=tk.X, pady=3)
        
        self.source_entry = tk.Entry(src_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.source_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.source_entry.insert(0, default_container_path)
        
        self.btn_src = tk.Button(src_inner, text="Browse...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_source)
        self.btn_src.pack(side=tk.RIGHT)

        # --- STAMP FILE SECTION ---
        self.stamp_card = tk.LabelFrame(self.main_frame, text=" Stamp Image Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        self.stamp_card.pack(fill=tk.X, pady=(0, 12))

        self.lbl2 = tk.Label(self.stamp_card, text="Fixed Stamp File Path (.png):", bg="#ffffff", font=("Arial", self.current_font_size, "bold"))
        self.lbl2.pack(anchor="w", pady=(2, 0))
        
        stamp_inner = tk.Frame(self.stamp_card, bg="#ffffff")
        stamp_inner.pack(fill=tk.X, pady=5)
        
        self.fixed_entry = tk.Entry(stamp_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.fixed_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.fixed_entry.insert(0, r"D:\barcodestame\new 2 stamp RB.png")
        
        self.btn_fx = tk.Button(stamp_inner, text="Browse...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_fixed_file)
        self.btn_fx.pack(side=tk.RIGHT)

        # --- OUTPUT DESTINATION SECTION ---
        default_output_nested = rf"D:\Scan\Main OutputFastOCR\OutputFastOCR_{today_date_str}\BarcodeandStamp"

        self.out_card = tk.LabelFrame(self.main_frame, text=" Output Destination Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
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
        self.open_folder_btn.pack(side=tk.LEFT)

        self.open_report_btn = tk.Button(output_ctrl_frame, text=" 📊 Open Report CSV ", bg="#e67e22", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=10, pady=6, command=self.open_duplicates_report)
        self.open_report_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.toggle_list_copy_btn = tk.Button(output_ctrl_frame, text=" 🚀 Start Auto-Copy (list_of_container) ", bg="#8e44ad", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=10, pady=6, command=self.toggle_list_copy_loop)
        self.toggle_list_copy_btn.pack(side=tk.LEFT, padx=(8, 0))

        # --- GOOGLE SHEETS (data_seals) AUTO-SYNC CONFIGURATION ---
        default_csv_path = rf"D:\Scan\Main OutputFastOCR\OutputFastOCR_{today_date_str}\Report\report.csv"

        self.seal_card = tk.LabelFrame(self.main_frame, text=" Google Sheets (data_seals) Auto-Sync Configuration ", bg="#ffffff", fg="#2c3e50", font=("Arial", 10, "bold"), padx=15, pady=10)
        self.seal_card.pack(fill=tk.X, pady=(0, 12))

        self.lbl_seal_src = tk.Label(self.seal_card, text="Select Source CSV Report File:", bg="#ffffff", font=("Arial", self.current_font_size, "bold"))
        self.lbl_seal_src.pack(anchor="w", pady=(2, 0))

        seal_src_inner = tk.Frame(self.seal_card, bg="#ffffff")
        seal_src_inner.pack(fill=tk.X, pady=3)

        self.seal_src_entry = tk.Entry(seal_src_inner, font=("Arial", self.current_font_size), relief=tk.SOLID, bd=1)
        self.seal_src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))
        self.seal_src_entry.insert(0, default_csv_path)
        
        self.btn_browse_seal_src = tk.Button(seal_src_inner, text="Browse Source...", font=("Arial", self.current_font_size), bg="#ecf0f1", command=self.select_source_seal_file)
        self.btn_browse_seal_src.pack(side=tk.RIGHT)

        sheet_mgmt_frame = tk.Frame(self.seal_card, bg="#ffffff")
        sheet_mgmt_frame.pack(fill=tk.X, pady=(6, 2))

        self.lbl_sheets_active = tk.Label(sheet_mgmt_frame, text="", bg="#ffffff", fg="#27ae60", font=("Arial", self.current_font_size, "bold"))
        self.lbl_sheets_active.pack(side=tk.LEFT, anchor="w")

        self.btn_manage_sheets = tk.Button(sheet_mgmt_frame, text=" ➕ Manage / Add Sheet Tabs ", bg="#2980b9", fg="white", font=("Arial", 9, "bold"), relief=tk.FLAT, padx=8, pady=4, command=self.open_sheet_manager_dialog)
        self.btn_manage_sheets.pack(side=tk.RIGHT, padx=(0, 4))

        self.btn_auto_mirror = tk.Button(sheet_mgmt_frame, text=" 🔄 Auto-Mirror Source Name ", bg="#8e44ad", fg="white", font=("Arial", 9, "bold"), relief=tk.FLAT, padx=8, pady=4, command=self.auto_mirror_source_sheet_name)
        self.btn_auto_mirror.pack(side=tk.RIGHT, padx=(4, 0))

        self.update_active_sheets_label()

        sheet_sync_ctrl_frame = tk.Frame(self.seal_card, bg="#ffffff")
        sheet_sync_ctrl_frame.pack(fill=tk.X, pady=(8, 4))

        tk.Label(sheet_sync_ctrl_frame, text="Loop Interval (Sec):", bg="#ffffff", font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        
        self.sheet_interval_entry = tk.Entry(sheet_sync_ctrl_frame, font=("Arial", 10), width=5, relief=tk.SOLID, bd=1)
        self.sheet_interval_entry.pack(side=tk.LEFT, padx=(0, 8))
        self.sheet_interval_entry.insert(0, "30")

        self.toggle_sheet_loop_btn = tk.Button(
            sheet_sync_ctrl_frame, 
            text=" ⏱️ Start Auto-Sync Loop ", 
            bg="#27ae60", 
            fg="white", 
            font=("Arial", 10, "bold"), 
            relief=tk.FLAT, 
            padx=10, 
            pady=6, 
            command=self.toggle_sheet_sync_loop
        )
        self.toggle_sheet_loop_btn.pack(side=tk.LEFT)

        # --- ACTION BUTTONS CONTAINER ---
        action_btns_frame = tk.Frame(self.main_frame, bg="#f4f6f7")
        action_btns_frame.pack(fill=tk.X, pady=(0, 12))

        self.watch_btn = tk.Button(action_btns_frame, text="Start Auto-Watch & Process", bg="#27ae60", fg="white", font=("Arial", 11, "bold"), bd=0, relief=tk.FLAT, command=self.toggle_watch)
        self.watch_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5), ipady=8)

        self.process_copy_btn = tk.Button(action_btns_frame, text="Start Auto-Copy & Process (Keep Source)", bg="#d35400", fg="white", font=("Arial", 11, "bold"), bd=0, relief=tk.FLAT, command=self.toggle_copy_processing)
        self.process_copy_btn.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(5, 0), ipady=8)

        # --- CONSOLE LOG BOX ---
        self.log_box = scrolledtext.ScrolledText(self.main_frame, font=("Consolas", self.current_font_size), height=10, state="normal", relief=tk.SOLID, bd=1)
        self.log_box.pack(fill=tk.BOTH, expand=True)

        self.update_output_file_count()

    def select_verify_source(self):
        path = filedialog.askdirectory()
        if path:
            self.verify_entry.delete(0, tk.END)
            self.verify_entry.insert(0, path)

    def toggle_processing_mode(self):
        if not self.verify_mode_active:
            verify_path = self.verify_entry.get().strip()
            loaded_regs = set()
            
            try:
                if verify_path and os.path.exists(verify_path):
                    if os.path.isdir(verify_path):
                        for root_dir, _, files in os.walk(verify_path):
                            for file in files:
                                if file.lower().endswith('.csv'):
                                    csv_path = os.path.join(root_dir, file)
                                    try:
                                        with open(csv_path, mode='r', encoding='utf-8-sig') as f:
                                            reader = csv.reader(f)
                                            for row in reader:
                                                for cell in row:
                                                    nums = re.findall(r'\d+', cell)
                                                    for n in nums:
                                                        if len(n) >= 4:
                                                            loaded_regs.add(n)
                                    except:
                                        pass
                                else:
                                    # Support image filenames (JPG/PNG) as register sources
                                    nums = re.findall(r'\d+', file)
                                    for n in nums:
                                        if len(n) >= 4:
                                            loaded_regs.add(n)
                    elif os.path.isfile(verify_path) and verify_path.lower().endswith('.csv'):
                        with open(verify_path, mode='r', encoding='utf-8-sig') as f:
                            reader = csv.reader(f)
                            for row in reader:
                                for cell in row:
                                    nums = re.findall(r'\d+', cell)
                                    for n in nums:
                                        if len(n) >= 4:
                                            loaded_regs.add(n)

                # Even if folder is empty or not yet populated, activate verify mode without blocking
                self.register_numbers_set = loaded_regs
                self.verify_mode_active = True
                self.mode_lbl.config(text="Current Mode: [ Verify Mode (Register & Letter M Active) ]", fg="#2ecc71")
                self.mode_toggle_btn.config(text=" 🔀 Switch to Container-Only Mode ", bg="#c0392b")
                self.log_box.insert(tk.END, f"Verify Mode activated. Loaded {len(loaded_regs)} register numbers from '{verify_path}'. Files must match register AND end with letter 'M'.\n")
                self.log_box.see(tk.END)
            except Exception as e:
                # Silent activation even if path has issues
                self.verify_mode_active = True
                self.mode_lbl.config(text="Current Mode: [ Verify Mode (Register & Letter M Active) ]", fg="#2ecc71")
                self.mode_toggle_btn.config(text=" 🔀 Switch to Container-Only Mode ", bg="#c0392b")
                self.log_box.insert(tk.END, f"Verify Mode activated (Path: {verify_path}).\n")
                self.log_box.see(tk.END)
        else:
            self.verify_mode_active = False
            self.register_numbers_set.clear()
            self.mode_lbl.config(text="Current Mode: [ Container-Only Mode ]", fg="#e67e22")
            self.mode_toggle_btn.config(text=" 🔀 Switch to Verify Mode (Register & Letter M) ", bg="#2980b9")
            self.log_box.insert(tk.END, "Switched back to Container-Only Mode.\n")
            self.log_box.see(tk.END)

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

    def open_duplicates_report(self):
        target_dir = self.output_entry.get().strip()
        report_path = os.path.join(target_dir, "Duplicate_containers", "duplicates_report.csv")
        if os.path.exists(report_path):
            subprocess.Popen(f'excel "{os.path.abspath(report_path)}"' if os.name == 'nt' else f'open "{os.path.abspath(report_path)}"')
        else:
            messagebox.showinfo("Report Notice", "The duplicates report CSV file does not exist yet (no duplicates processed yet).")

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
                
                if source_dir and output_dir and os.path.exists(output_dir) and os.path.exists(source_dir):
                    target_list_dir = os.path.join(output_dir, "list_of_container")
                    dup_folder = os.path.join(output_dir, "Duplicate_containers")
                    os.makedirs(target_list_dir, exist_ok=True)
                    os.makedirs(dup_folder, exist_ok=True)
                    
                    report_path = os.path.join(dup_folder, "duplicates_report.csv")
                    if not os.path.exists(report_path):
                        with open(report_path, mode='w', newline='', encoding='utf-8') as f:
                            writer = csv.writer(f)
                            writer.writerow(["Timestamp", "Duplicate File", "Container Number", "Original File"])

                    files = [f for f in os.listdir(source_dir) if f.lower().endswith('.jpg')]
                    for file_name in files:
                        src_file_path = os.path.join(source_dir, file_name)
                        dest_file_path = os.path.join(target_list_dir, file_name)
                        if not os.path.exists(dest_file_path):
                            shutil.copy2(src_file_path, dest_file_path)
            except Exception:
                pass
            time.sleep(0.5)

    def auto_mirror_source_sheet_name(self):
        src_file = self.seal_src_entry.get()
        if not src_file:
            messagebox.showwarning("Warning", "Please select a source CSV file first.")
            return
        
        base_name = os.path.splitext(os.path.basename(src_file))[0].strip()
        if base_name:
            creds_path = r"D:\barcodestame\credentials.json"
            try:
                client = gspread.service_account(filename=creds_path)
                spreadsheet = client.open("data_seals")
                try:
                    spreadsheet.worksheet(base_name)
                except gspread.exceptions.WorksheetNotFound:
                    spreadsheet.add_worksheet(title=base_name, rows=1000, cols=20)
                    self.log_box.insert(tk.END, f"Auto-created online sheet tab '{base_name}' in Google Drive.\n")
            except Exception as e:
                self.log_box.insert(tk.END, f"Warning: Could not auto-create online tab '{base_name}': {e}\n")

            if base_name not in self.target_sheets:
                self.target_sheets.append(base_name)
                self.update_active_sheets_label()
                self.log_box.insert(tk.END, f"Auto-mirrored source name into managed list: '{base_name}'\n")
                self.log_box.see(tk.END)
                messagebox.showinfo("Auto-Mirrored", f"Successfully matched and added source name '{base_name}' to your managed sheet list!")
            else:
                messagebox.showinfo("Already Active", f"Sheet tab '{base_name}' is already in your managed list.")

    def open_sheet_manager_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Manage Sheet Tabs")
        dlg.geometry("400x380")
        dlg.configure(bg="#f4f6f7")
        dlg.grab_set()

        tk.Label(dlg, text="Managed Sheet Tabs List:", bg="#f4f6f7", font=("Arial", 11, "bold")).pack(pady=(15, 5))
        tk.Label(dlg, text="(Adding a sheet here will also create it in your Google Spreadsheet)", bg="#f4f6f7", fg="#7f8c8d", font=("Arial", 9, "italic")).pack(pady=(0, 5))

        list_frame = tk.Frame(dlg, bg="#f4f6f7")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)

        sheet_listbox = tk.Listbox(list_frame, font=("Arial", 11), selectmode=tk.SINGLE)
        sheet_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for s in self.target_sheets:
            sheet_listbox.insert(tk.END, s)

        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL, command=sheet_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        sheet_listbox.config(yscrollcommand=scrollbar.set)

        ctrl_frame = tk.Frame(dlg, bg="#f4f6f7")
        ctrl_frame.pack(fill=tk.X, padx=20, pady=10)

        def add_sheet():
            new_name = simpledialog.askstring("Add Sheet Tab", "Enter sheet tab name (e.g., report, rey, neath):", parent=dlg)
            if new_name:
                clean_name = new_name.strip()
                if clean_name:
                    creds_path = r"D:\barcodestame\credentials.json"
                    try:
                        client = gspread.service_account(filename=creds_path)
                        spreadsheet = client.open("data_seals")
                        try:
                            spreadsheet.worksheet(clean_name)
                        except gspread.exceptions.WorksheetNotFound:
                            spreadsheet.add_worksheet(title=clean_name, rows=1000, cols=20)
                            self.log_box.insert(tk.END, f"Successfully created online sheet tab '{clean_name}' in Google Drive.\n")
                    except Exception as e:
                        self.log_box.insert(tk.END, f"Warning: Could not create online sheet tab '{clean_name}': {e}\n")

                    if clean_name not in self.target_sheets:
                        self.target_sheets.append(clean_name)
                        sheet_listbox.insert(tk.END, clean_name)
                        self.update_active_sheets_label()
                        self.log_box.insert(tk.END, f"Added sheet tab to local list: '{clean_name}'\n")
                        self.log_box.see(tk.END)

        def remove_sheet():
            selected_idx = sheet_listbox.curselection()
            if selected_idx:
                idx = selected_idx[0]
                sheet_to_remove = sheet_listbox.get(idx)
                if len(self.target_sheets) > 1:
                    self.target_sheets.remove(sheet_to_remove)
                    sheet_listbox.delete(idx)
                    self.update_active_sheets_label()
                    self.log_box.insert(tk.END, f"Removed sheet tab from list: '{sheet_to_remove}'\n")
                    self.log_box.see(tk.END)
                else:
                    messagebox.showwarning("Warning", "You must keep at least one sheet tab in the list.", parent=dlg)

        btn_add = tk.Button(ctrl_frame, text=" ➕ Add Tab & Create Online ", bg="#27ae60", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=10, pady=5, command=add_sheet)
        btn_add.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))

        btn_remove = tk.Button(ctrl_frame, text=" ❌ Remove Selected ", bg="#e74c3c", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=10, pady=5, command=remove_sheet)
        btn_remove.pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=(5, 0))

        btn_close = tk.Button(dlg, text="Done / Save", bg="#34495e", fg="white", font=("Arial", 10, "bold"), relief=tk.FLAT, padx=20, pady=6, command=dlg.destroy)
        btn_close.pack(pady=(0, 15))

    def update_active_sheets_label(self):
        sheets_str = ", ".join(self.target_sheets)
        self.lbl_sheets_active.config(text=f"Target Sheets Active: {sheets_str}")

    def select_source_seal_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")])
        if file_path:
            self.seal_src_entry.delete(0, tk.END)
            self.seal_src_entry.insert(0, file_path)
            self.log_box.insert(tk.END, f"Source CSV report selected: {file_path}\n")
            self.log_box.see(tk.END)

    def sync_data_to_google_sheet(self):
        src_file = self.seal_src_entry.get()
        if not src_file or not os.path.exists(src_file):
            return False, "Source CSV report file not found."

        source_base_name = os.path.splitext(os.path.basename(src_file))[0].strip().lower()
        creds_path = r"D:\barcodestame\credentials.json"
        if not os.path.exists(creds_path):
            return False, "Missing credentials.json file."

        try:
            client = gspread.service_account(filename=creds_path)
            spreadsheet = client.open("data_seals")
            
            csv_rows = []
            with open(src_file, mode='r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                for row in reader:
                    if row:
                        csv_rows.append(row)

            if not csv_rows:
                return False, "CSV file is empty."

            success_count = 0
            for sheet_name in self.target_sheets:
                clean_sheet_name = sheet_name.strip().lower()
                if clean_sheet_name != source_base_name:
                    continue

                try:
                    try:
                        sheet = spreadsheet.worksheet(sheet_name)
                    except gspread.exceptions.WorksheetNotFound:
                        sheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=20)

                    existing_data = sheet.get_all_values()
                    if not existing_data or all(not any(row) for row in existing_data):
                        sheet.update(range_name='A1', values=csv_rows)
                        success_count += 1
                    else:
                        existing_set = {tuple(row) for row in existing_data}
                        new_rows = [row for row in csv_rows if tuple(row) not in existing_set]
                        if new_rows:
                            sheet.append_rows(new_rows)
                            success_count += 1
                except Exception:
                    pass
            return True, "Synced"
        except Exception as e:
            return False, str(e)

    def toggle_sheet_sync_loop(self):
        if not self.is_sheet_sync_looping:
            try:
                interval_val = float(self.sheet_interval_entry.get().strip())
                if interval_val <= 0:
                    raise ValueError()
            except ValueError:
                messagebox.showerror("Invalid Interval", "Please enter a valid number of seconds.")
                return

            self.is_sheet_sync_looping = True
            self.toggle_sheet_loop_btn.config(text=f"⏹️ Stop Auto-Sync ({interval_val}s)", bg="#c0392b")
            self.log_box.insert(tk.END, f"Google Sheet Auto-Sync loop started (every {interval_val} seconds).\n")
            self.log_box.see(tk.END)

            threading.Thread(target=self.sheet_sync_loop_worker, args=(interval_val,), daemon=True).start()
        else:
            self.is_sheet_sync_looping = False
            self.toggle_sheet_loop_btn.config(text="⏱️ Start Auto-Sync Loop", bg="#27ae60")
            self.log_box.insert(tk.END, "Google Sheet Auto-Sync loop stopped.\n")
            self.log_box.see(tk.END)

    def sheet_sync_loop_worker(self, interval_seconds):
        while self.is_sheet_sync_looping:
            try:
                self.sync_data_to_google_sheet()
            except Exception:
                pass
            time.sleep(interval_seconds)

    def toggle_copy_processing(self):
        if not self.is_copy_processing:
            source_dir = self.source_entry.get().strip()
            output_dir = self.output_entry.get().strip()
            
            if not source_dir or not output_dir:
                messagebox.showerror("Error", "Please specify Source folder and Output directory.")
                return

            parent_dir = os.path.dirname(output_dir)
            if not os.path.exists(parent_dir):
                messagebox.showerror("Parent Folder Missing", f"Required parent folder does not exist:\n\n{parent_dir}")
                return

            os.makedirs(output_dir, exist_ok=True)
            os.makedirs(os.path.join(output_dir, "list_of_container"), exist_ok=True)
            dup_folder = os.path.join(output_dir, "Duplicate_containers")
            os.makedirs(dup_folder, exist_ok=True)
            
            report_path = os.path.join(dup_folder, "duplicates_report.csv")
            try:
                if not os.path.exists(report_path):
                    with open(report_path, mode='w', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow(["Timestamp", "Duplicate File", "Container Number", "Original File"])
            except PermissionError:
                messagebox.showerror("Excel File Locked", "Please close Excel / duplicates_report.csv before starting the app!")
                return

            self.session_output_dir = output_dir
            self.is_copy_processing = True
            self.process_copy_btn.config(text="Stop Auto-Copy & Process", bg="#c0392b")
            self.log_box.insert(tk.END, f"Auto-Copy & Process started (Continuous Loop, Keep Source). Output: {output_dir}\n")
            self.log_box.see(tk.END)

            threading.Thread(target=self.copy_processing_loop_worker, daemon=True).start()
        else:
            self.is_copy_processing = False
            self.process_copy_btn.config(text="Start Auto-Copy & Process (Keep Source)", bg="#d35400")
            self.log_box.insert(tk.END, "Auto-Copy & Process stopped.\n")
            self.log_box.see(tk.END)

    def copy_processing_loop_worker(self):
        source_dir = self.source_entry.get()
        fixed_img = self.load_stamp_image()

        while self.is_copy_processing:
            try:
                if os.path.exists(source_dir):
                    os.makedirs(self.session_output_dir, exist_ok=True)
                    os.makedirs(os.path.join(self.session_output_dir, "list_of_container"), exist_ok=True)
                    os.makedirs(os.path.join(self.session_output_dir, "Duplicate_containers"), exist_ok=True)
                    
                    all_files = [f for f in os.listdir(source_dir) if f.lower().endswith('.jpg')]
                    container_files = [f for f in all_files if f not in self.processed_source_files]
                    
                    if container_files:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                            futures = {
                                executor.submit(process_single_file, file_name, source_dir, self.session_output_dir, fixed_img, copy_only=True, seen_containers=self.seen_containers, first_seen_files=self.first_seen_files, verify_mode=self.verify_mode_active, register_numbers=self.register_numbers_set): file_name 
                                for file_name in container_files
                            }
                            
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
                                self.update_output_file_count()
            except Exception as e:
                pass
            time.sleep(1.0)

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
            self.seal_card.pack_forget()
            self.expand_log_btn.config(text=" 📜 Restore View ", bg="#7f8c8d")
            self.log_box.config(height=32)
        else:
            self.src_card.pack(fill=tk.X, pady=(0, 12))
            self.stamp_card.pack(fill=tk.X, pady=(0, 12))
            self.out_card.pack(fill=tk.X, pady=(0, 12))
            self.seal_card.pack(fill=tk.X, pady=(0, 12))
            
            self.watch_btn.master.pack_forget()
            self.log_box.pack_forget()
            
            self.watch_btn.master.pack(fill=tk.X, pady=(0, 12))
            self.log_box.pack(fill=tk.BOTH, expand=True)
            
            self.expand_log_btn.config(text=" 📜 Expand Log ", bg="#34495e")
            self.log_box.config(height=10)

    def open_current_output_folder(self):
        target_dir = self.output_entry.get()
        if target_dir and os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)
            subprocess.Popen(f'explorer "{os.path.abspath(target_dir)}"')
        else:
            messagebox.showinfo("Folder Notice", "The output folder path does not exist yet.")

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
        
        for ent in [self.source_entry, self.fixed_entry, self.output_entry, self.seal_src_entry, self.sheet_interval_entry, self.verify_entry]:
            ent.config(font=f_norm)
        for btn in [self.btn_src, self.btn_fx, self.btn_out, self.btn_browse_seal_src, self.btn_verify_src]:
            btn.config(font=f_norm)
            
        self.log_box.config(font=f_log)

    def select_source(self):
        folder = filedialog.askdirectory()
        if folder:
            self.source_entry.delete(0, tk.END)
            self.source_entry.insert(0, folder)

    def select_fixed_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png;*.jpg;*.jpeg")])
        if file_path:
            self.fixed_entry.delete(0, tk.END)
            self.fixed_entry.insert(0, file_path)

    def select_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, folder)

    def auto_create_barcode_stamp_folder(self):
        target_dir = self.output_entry.get()
        if not target_dir:
            messagebox.showwarning("Warning", "Output directory path is empty.")
            return

        parent_dir = os.path.dirname(target_dir)
        if not os.path.exists(parent_dir):
            messagebox.showerror("Parent Folder Missing", f"Parent folder does not exist:\n\n{parent_dir}")
            return

        try:
            os.makedirs(target_dir, exist_ok=True)
            os.makedirs(os.path.join(target_dir, "list_of_container"), exist_ok=True)
            dup_folder = os.path.join(target_dir, "Duplicate_containers")
            os.makedirs(dup_folder, exist_ok=True)
            
            report_path = os.path.join(dup_folder, "duplicates_report.csv")
            if not os.path.exists(report_path):
                with open(report_path, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Timestamp", "Duplicate File", "Container Number", "Original File"])

            messagebox.showinfo("Success", f"Folder and subfolders created successfully:\n{target_dir}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not create folder:\n{e}")

    def reset_history(self):
        self.count_processed = 0
        self.count_already_stamped = 0
        self.count_errors = 0
        self.seen_containers.clear()
        self.first_seen_files.clear()
        self.processed_source_files.clear()
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
            except Exception:
                pass
        return None

    def toggle_watch(self):
        if not self.is_watching:
            target_output_dir = self.output_entry.get()
            source = self.source_entry.get()
            if not source or not target_output_dir:
                messagebox.showerror("Missing Information", "Please specify Source folder and Output directory.")
                return
            
            parent_dir = os.path.dirname(target_output_dir)
            if not os.path.exists(parent_dir):
                messagebox.showerror("Parent Folder Missing", f"Parent folder does not exist:\n\n{parent_dir}")
                return

            self.session_output_dir = target_output_dir
            os.makedirs(self.session_output_dir, exist_ok=True)
            os.makedirs(os.path.join(self.session_output_dir, "list_of_container"), exist_ok=True)
            dup_folder = os.path.join(self.session_output_dir, "Duplicate_containers")
            os.makedirs(dup_folder, exist_ok=True)
            
            report_path = os.path.join(dup_folder, "duplicates_report.csv")
            try:
                if not os.path.exists(report_path):
                    with open(report_path, mode='w', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow(["Timestamp", "Duplicate File", "Container Number", "Original File"])
            except PermissionError:
                messagebox.showerror("Excel File Locked", "Please close Excel / duplicates_report.csv before starting the app!")
                return
            
            self.is_watching = True
            self.status_canvas.itemconfig(self.status_circle, fill="#e74c3c")
            self.watch_btn.config(text="Stop Auto-Watch Mode", bg="#c0392b")
            self.log_box.insert(tk.END, f"Auto-Watch started (Continuous Loop). Output: {self.session_output_dir}\n")
            self.log_box.see(tk.END)
            
            threading.Thread(target=self.watch_folder_loop, daemon=True).start()
        else:
            self.is_watching = False
            self.status_canvas.itemconfig(self.status_circle, fill="#27ae60")
            self.watch_btn.config(text="Start Auto-Watch & Process", bg="#27ae60")
            self.log_box.insert(tk.END, "Auto-Watch mode stopped.\n")
            self.log_box.see(tk.END)

    def watch_folder_loop(self):
        source_dir = self.source_entry.get()
        fixed_img = self.load_stamp_image()

        while self.is_watching:
            try:
                if os.path.exists(source_dir):
                    os.makedirs(self.session_output_dir, exist_ok=True)
                    os.makedirs(os.path.join(self.session_output_dir, "list_of_container"), exist_ok=True)
                    os.makedirs(os.path.join(self.session_output_dir, "Duplicate_containers"), exist_ok=True)
                    
                    all_files = [f for f in os.listdir(source_dir) if f.lower().endswith('.jpg')]
                    container_files = [f for f in all_files if f not in self.processed_source_files]
                    
                    if container_files:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                            futures = {
                                executor.submit(process_single_file, file_name, source_dir, self.session_output_dir, fixed_img, copy_only=False, seen_containers=self.seen_containers, first_seen_files=self.first_seen_files, verify_mode=self.verify_mode_active, register_numbers=self.register_numbers_set): file_name 
                                for file_name in container_files
                            }
                            
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
                                self.update_output_file_count()
            except Exception as e:
                pass
            time.sleep(1.0)

if __name__ == "__main__":
    root = tk.Tk()
    app = BarcodeApp(root)
    root.mainloop()