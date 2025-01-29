import tkinter as tk
from tkinter import messagebox, filedialog
import pystray
from PIL import Image, ImageDraw, ImageFilter, ImageTk, ImageOps
import threading
import time
import json
import os
import sys

from playsound import playsound  # or use winsound/simpleaudio if you prefer

# NEW: import appdirs for cross-platform user-data location
from appdirs import user_data_dir

def resource_path(relative_path):
    """
    Used if you're embedding 'bottle.png' or 'tray_icon.png' via PyInstaller
    with --add-data. This locates them at runtime.
    """
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def get_config_path():
    """
    Return the path to our water_data.json in the OS-specific user data directory.
    e.g., on Windows:  C:\\Users\\<USER>\\AppData\\Roaming\\WaterTrackerApp\\water_data.json
         on macOS:    ~/Library/Application Support/WaterTrackerApp/water_data.json
         on Linux:     ~/.local/share/WaterTrackerApp/water_data.json
    """
    # You can change "WaterTrackerApp" if you like.
    data_dir = user_data_dir(appname="WaterTrackerApp", appauthor=False)
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "water_data.json")


def generate_bottle_images_in_memory(input_path, blur_radius=1, threshold=128):
    """
    1) Load the bottle as grayscale, blur & threshold to get a mask silhouette (white=bottle).
    2) Possibly invert if silhouette is black.
    3) Create two in‐memory images:
       - mask_img: a black‐and‐white silhouette
       - empty_img: a gray silhouette (empty bottle), transparent outside
    Returns (mask_img, empty_img) as PIL Image objects in memory.
    """
    img = Image.open(input_path).convert("L")
    img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    img = img.point(lambda p: 255 if p > threshold else 0)

    # Possibly invert if silhouette is black and background is white
    white_count = sum(1 for p in img.getdata() if p == 255)
    black_count = sum(1 for p in img.getdata() if p == 0)
    if black_count > white_count:
        img = ImageOps.invert(img)

    # 'img' is now a mask: white = bottle, black = background
    mask_img = img.copy()  # Keep a copy for the alpha mask

    # Make a gray silhouette for an "empty" bottle
    width, height = img.size
    empty_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    new_data = []
    gray_color = (150, 150, 150, 255)
    for px in img.getdata():
        if px == 255:
            new_data.append(gray_color)
        else:
            new_data.append((0, 0, 0, 0))  # Transparent background
    empty_img.putdata(new_data)

    return (mask_img, empty_img)


class WaterTrackerApp:
    CAPACITY = 3000

    def __init__(self, bottle_path="bottle.png"):
        # Create the Tk root
        self.root = tk.Tk()
        self.root.title("Water Tracker")
        self.root.configure(bg="#F0F0F0")

        # Convert path if using PyInstaller to embed bottle.png
        bottle_path = resource_path(bottle_path)

        # Generate the silhouettes in memory (no writing to disk)
        mask_img, empty_img = generate_bottle_images_in_memory(bottle_path)

        # We'll store them for use in 'update_filled_bottle'
        self.bottle_mask = mask_img.convert("L")      # grayscale mask
        self.empty_bottle = empty_img.convert("RGBA") # the empty gray silhouette

        # Defaults
        self.total_consumed = 0

        # This will store the path to the user's custom sound
        self.custom_sound_path = tk.StringVar(value="")

        # Minimizing to tray?
        self.minimize_to_tray = tk.BooleanVar(value=False)

        # Reminder settings
        self.reminder_running = False
        self.reminder_minutes_var = tk.IntVar(value=1)
        self.next_reminder_time = 0

        # Load saved data (if any)
        self.load_data()

        # Build the UI
        self.create_main_layout()
        self.update_sound_label()

        # For tray minimization
        self.tray_icon = None
        self.tray_thread = None

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.root.mainloop()

    def create_main_layout(self):
        """Build the main UI."""
        left_frame = tk.Frame(self.root, bg="#F0F0F0")
        left_frame.pack(side="left", padx=10, pady=10)

        self.bottle_label = tk.Label(left_frame, bg="#F0F0F0")
        self.bottle_label.pack()

        right_frame = tk.Frame(self.root, bg="#F0F0F0")
        right_frame.pack(side="right", padx=10, pady=10)

        # Water status
        self.status_label = tk.Label(
            right_frame,
            text=f"Consumed: {self.total_consumed} ml",
            font=("Arial", 14),
            bg="#F0F0F0"
        )
        self.status_label.pack(pady=5)

        tk.Button(right_frame, text="+150 ml", command=self.add_150).pack(pady=5)

        custom_frame = tk.Frame(right_frame, bg="#F0F0F0")
        custom_frame.pack(pady=5)

        tk.Label(custom_frame, text="Custom amount (ml):", bg="#F0F0F0").pack(side=tk.LEFT)
        self.custom_entry = tk.Entry(custom_frame, width=5)
        self.custom_entry.pack(side=tk.LEFT, padx=5)
        tk.Button(custom_frame, text="Add", command=self.add_custom).pack(side=tk.LEFT)

        tk.Button(right_frame, text="Reset Water", command=self.reset_consumption).pack(pady=5)

        tray_check = tk.Checkbutton(
            right_frame,
            text="Minimize to Tray on Close",
            variable=self.minimize_to_tray,
            bg="#F0F0F0"
        )
        tray_check.pack(pady=10)

        reminder_frame = tk.Frame(right_frame, bg="#F0F0F0")
        reminder_frame.pack(pady=5)
        tk.Label(reminder_frame, text="Reminder Interval (min):", bg="#F0F0F0").pack(side=tk.LEFT)
        tk.Spinbox(reminder_frame, from_=1, to=180,
                   textvariable=self.reminder_minutes_var, width=5).pack(side=tk.LEFT, padx=5)

        self.reminder_button = tk.Button(
            right_frame,
            text="Start Reminder",
            command=self.toggle_reminder
        )
        self.reminder_button.pack(pady=5)

        self.countdown_label = tk.Label(
            right_frame,
            text="No reminder active.",
            bg="#F0F0F0",
            font=("Arial", 10)
        )
        self.countdown_label.pack(pady=5)

        # Custom sound
        sound_frame = tk.Frame(right_frame, bg="#F0F0F0")
        sound_frame.pack(pady=5)

        tk.Label(sound_frame, text="Custom Sound:", bg="#F0F0F0").pack(side=tk.LEFT)
        self.sound_label = tk.Label(sound_frame, text="", bg="#F0F0F0", width=20, anchor="w")
        self.sound_label.pack(side=tk.LEFT, padx=5)

        browse_btn = tk.Button(sound_frame, text="Browse", command=self.choose_sound_file)
        browse_btn.pack(side=tk.LEFT)

        # Draw bottle initially
        self.update_filled_bottle()

    def choose_sound_file(self):
        """Open a file dialog to pick an audio file."""
        path = filedialog.askopenfilename(
            title="Select Reminder Sound",
            filetypes=[("Audio Files", "*.mp3 *.wav"), ("All Files", "*.*")]
        )
        if path:
            self.custom_sound_path.set(path)
            self.update_sound_label()

    def update_sound_label(self):
        """Show the currently chosen audio file or 'No sound selected'."""
        sound_file = self.custom_sound_path.get()
        if sound_file:
            self.sound_label.config(text=os.path.basename(sound_file))
        else:
            self.sound_label.config(text="No sound selected")

    def add_150(self):
        self.total_consumed += 150
        self.update_status()
        self.update_filled_bottle()

    def add_custom(self):
        try:
            amount = int(self.custom_entry.get())
            if amount > 0:
                self.total_consumed += amount
                self.update_status()
                self.update_filled_bottle()
                self.custom_entry.delete(0, tk.END)
            else:
                messagebox.showwarning("Invalid Entry", "Please enter a positive integer.")
        except ValueError:
            messagebox.showwarning("Invalid Entry", "Please enter a valid integer.")

    def reset_consumption(self):
        if messagebox.askyesno("Reset", "Reset all water consumption to 0?"):
            self.total_consumed = 0
            self.update_status()
            self.update_filled_bottle()

    def update_status(self):
        self.status_label.config(text=f"Consumed: {self.total_consumed} ml")

    def update_filled_bottle(self):
        """
        Use self.bottle_mask (L-mode) as the alpha mask to ensure the water fill
        stays inside the silhouette, and self.empty_bottle as the base.
        """
        ratio = min(1.0, self.total_consumed / self.CAPACITY)
        w, h = self.empty_bottle.size
        fill_height = int(h * ratio)

        fill_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(fill_layer)
        draw.rectangle([0, h - fill_height, w, h], fill=(0, 0, 255, 200))
        fill_layer.putalpha(self.bottle_mask)

        filled_bottle = Image.alpha_composite(self.empty_bottle, fill_layer)
        self.tk_bottle = ImageTk.PhotoImage(filled_bottle)
        self.bottle_label.config(image=self.tk_bottle)
        self.bottle_label.image = self.tk_bottle

    def toggle_reminder(self):
        if self.reminder_running:
            self.reminder_running = False
            self.reminder_button.config(text="Start Reminder")
            self.countdown_label.config(text="No reminder active.")
        else:
            self.reminder_running = True
            self.reminder_button.config(text="Stop Reminder")
            interval = max(1, self.reminder_minutes_var.get())
            self.next_reminder_time = time.time() + interval * 60
            self.update_countdown()

    def update_countdown(self):
        """Live countdown until the next reminder beep."""
        if not self.reminder_running:
            return
        time_left = int(self.next_reminder_time - time.time())
        if time_left <= 0:
            self.play_reminder_sound()
            interval = max(1, self.reminder_minutes_var.get())
            self.next_reminder_time = time.time() + interval * 60
            time_left = int(self.next_reminder_time - time.time())

        mins = time_left // 60
        secs = time_left % 60
        self.countdown_label.config(text=f"Next reminder in: {mins}:{secs:02d}")

        self.root.after(1000, self.update_countdown)

    def play_reminder_sound(self):
        sound_file = self.custom_sound_path.get().strip()
        if sound_file:
            try:
                playsound(sound_file, block=False)
            except Exception as e:
                messagebox.showwarning("Sound Error", f"Could not play custom sound:\n{e}")
                self.root.bell()
        else:
            self.root.bell()

    def on_closing(self):
        """Save data, then close or minimize."""
        self.save_data()
        if self.minimize_to_tray.get():
            self.hide_window()
        else:
            self.cleanup_tray_icon()
            self.root.destroy()

    def hide_window(self):
        self.root.withdraw()
        if not self.tray_icon:
            self.tray_thread = threading.Thread(target=self.setup_tray_icon)
            self.tray_thread.daemon = True
            self.tray_thread.start()

    def setup_tray_icon(self):
        icon_path = resource_path("tray_icon.png")  # if you have a tray icon embedded
        icon_image = Image.open(icon_path)
        menu = pystray.Menu(
            pystray.MenuItem("Show Water Tracker", self.show_window),
            pystray.MenuItem("Exit", self.exit_app)
        )
        self.tray_icon = pystray.Icon("WaterTracker", icon_image, "Water Tracker", menu)
        self.tray_icon.run()

    def show_window(self, icon=None, item=None):
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.deiconify()

    def exit_app(self, icon=None, item=None):
        self.save_data()
        self.cleanup_tray_icon()
        self.root.destroy()

    def cleanup_tray_icon(self):
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None

    #
    # ----------- CHANGED SECTIONS BELOW: store data in appdirs user_data_dir -----------
    #

    def load_data(self):
        """
        Load previous water consumption, sound path, reminder state
        from a hidden file in the user's app data directory.
        """
        config_path = get_config_path()  # e.g., %APPDATA%/WaterTrackerApp/water_data.json
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                    self.total_consumed = saved.get("total_consumed", 0)
                    custom_sound = saved.get("custom_sound", "")
                    self.custom_sound_path.set(custom_sound)
                    interval = saved.get("reminder_interval", 0)
                    self.reminder_minutes_var.set(interval)
            except:
                pass

    def save_data(self):
        """
        Save current water consumption, sound path, reminder state
        to a hidden file in the user's app data directory.
        """
        data = {
            "total_consumed": self.total_consumed,
            "custom_sound": self.custom_sound_path.get().strip(),
            "reminder_interval": self.reminder_minutes_var.get(),
        }
        config_path = get_config_path()
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        except:
            pass


if __name__ == "__main__":
    WaterTrackerApp("bottle.png")
