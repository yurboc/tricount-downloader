import requests
import json
import uuid
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
import rsa
import openpyxl
import csv
from tqdm import tqdm


class TricountAPI:
    def __init__(self):
        self.base_url = "https://api.tricount.bunq.com"
        self.app_installation_id = str(uuid.uuid4())
        self.public_key, self.private_key = rsa.newkeys(2048)
        self.rsa_public_key_pem = self.public_key.save_pkcs1(format="PEM").decode()
        self.headers = {
            "User-Agent": "com.bunq.tricount.android:RELEASE:7.0.7:3174:ANDROID:13:C",
            "app-id": self.app_installation_id,
            "X-Bunq-Client-Request-Id": "049bfcdf-6ae4-4cee-af7b-45da31ea85d0",
        }
        self.auth_token = None
        self.user_id = None

    def authenticate(self):
        auth_url = f"{self.base_url}/v1/session-registry-installation"
        auth_payload = {
            "app_installation_uuid": self.app_installation_id,
            "client_public_key": self.rsa_public_key_pem,
            "device_description": "Android",
        }
        response = requests.post(auth_url, json=auth_payload, headers=self.headers)
        response.raise_for_status()
        auth_data = response.json()

        response_items = auth_data["Response"]
        self.auth_token = next(
            item["Token"]["token"] for item in response_items if "Token" in item
        )
        self.user_id = next(
            item["UserPerson"]["id"] for item in response_items if "UserPerson" in item
        )
        self.headers["X-Bunq-Client-Authentication"] = self.auth_token

    def fetch_tricount_data(self, tricount_key):
        tricount_url = f"{self.base_url}/v1/user/{self.user_id}/registry?public_identifier_token={tricount_key}"
        response = requests.get(tricount_url, headers=self.headers)
        response.raise_for_status()
        return response.json()


class TricountHandler:
    @staticmethod
    def get_tricount_title(data):
        return data["Response"][0]["Registry"]["title"]

    @staticmethod
    def parse_tricount_data(data):
        registry = data["Response"][0]["Registry"]
        memberships = [
            {
                "Name": m["RegistryMembershipNonUser"]["alias"]["display_name"],
            }
            for m in registry["memberships"]
        ]

        transactions = []
        for entry in registry["all_registry_entry"]:
            transaction = entry["RegistryEntry"]
            type_transaction = transaction["type_transaction"]
            who_paid = transaction["membership_owned"]["RegistryMembershipNonUser"][
                "alias"
            ]["display_name"]
            total = float(transaction["amount"]["value"]) * -1
            currency = transaction["amount"]["currency"]
            description = transaction.get("description", "")
            when = transaction["date"]
            shares = {
                alloc["membership"]["RegistryMembershipNonUser"]["alias"][
                    "display_name"
                ]: abs(float(alloc["amount"]["value"]))
                for alloc in transaction["allocations"]
            }
            category = transaction["category"]
            attachments = transaction.get("attachment", [])

            transactions.append(
                {
                    "Type": type_transaction,
                    "Who Paid": who_paid,
                    "Total": total,
                    "Currency": currency,
                    "Description": description,
                    "When": when,
                    "Shares": shares,
                    "Category": category,
                    "Attachments": attachments,
                }
            )

        return memberships, transactions

    @staticmethod
    def download_attachments(transactions, download_folder):
        os.makedirs(download_folder, exist_ok=True)
        file_counter = 1
        total_files = sum(
            len(transaction["Attachments"]) for transaction in transactions
        )
        print(f"Total Attachments: {total_files}")

        if total_files == 0:
            return

        with tqdm(total=total_files, desc="Downloading attachments") as progress_bar:
            for transaction in transactions:
                attachment_files = []
                for attach in transaction["Attachments"]:
                    if "urls" in attach and attach["urls"]:
                        url = attach["urls"][0]["url"]
                        extension = os.path.splitext(url.split("?")[0])[1] or ".file"
                        file_name = f"receipt_{file_counter}{extension}"
                        file_path = os.path.join(download_folder, file_name)
                        TricountHandler.download_file(url, file_path)
                        attachment_files.append(file_name)
                        file_counter += 1
                        progress_bar.update(1)
                transaction["File Names"] = ", ".join(attachment_files)

    @staticmethod
    def download_file(url, file_path):
        response = requests.get(url)
        response.raise_for_status()
        with open(file_path, "wb") as file:
            file.write(response.content)

    @staticmethod
    def prepare_transaction_data(transaction):
        """
        Helper method to prepare the data for each transaction.
        Extracts involved people, formatted date, and attachment URLs.
        """
        # List of involved people involved in the transaction
        involved = ", ".join(
            [name for name, amount in transaction["Shares"].items() if amount > 0]
        )

        # Prepare the row data for the transaction
        row_data = [
            transaction["Who Paid"],
            transaction["Total"],
            transaction["Currency"],
            transaction["Description"],
            datetime.strptime(transaction["When"], "%Y-%m-%d %H:%M:%S.%f").strftime(
                "%Y-%m-%d"
            ),
            involved,
            transaction.get("File Names", ""),
            ", ".join(
                [
                    attach["urls"][0]["url"]
                    for attach in transaction["Attachments"]
                    if "urls" in attach and attach["urls"]
                ]
            ),
            transaction["Category"],
        ]

        return row_data

    @staticmethod
    def prepare_sesterce_transaction_data(transaction, members):
        """
        Helper method to prepare the data for each transaction in the sesterce format.
        A row contains:
        Date, Title,
        Paid by Member A, Paid by Member B, ... ,
        Paid for Member A, Paid for Member B, ... ,
        Currency, Category
        """
        # create Paid by data
        paid_by = [0.0] * len(members)
        payer = transaction["Who Paid"]
        paid_by[members.index(payer)] = transaction["Total"]

        # create Paid for data
        paid_for = [0.0] * len(members)
        # paid_for_member is the name of the person that is involved in the transaction and didn't pay
        for paid_for_member, amount in transaction["Shares"].items():
            paid_for[members.index(paid_for_member)] = amount

        # Determine the category based on the transaction type
        type_transaction = transaction["Type"]
        category = ""  # Default empty

        if type_transaction == "BALANCE":
            category = "Money Transfer"
        elif type_transaction == "INCOME":
            # Negate paid_for values for income
            paid_for = [-amount for amount in paid_for]
            category = (
                transaction["Category"]
                if transaction["Category"] != "UNCATEGORIZED"
                else ""
            )
        elif type_transaction == "NORMAL":
            # Use the category if present
            category = (
                transaction["Category"]
                if transaction["Category"] != "UNCATEGORIZED"
                else ""
            )

        # Prepare the row data for the transaction
        row_data = [
            datetime.strptime(transaction["When"], "%Y-%m-%d %H:%M:%S.%f").strftime(
                "%Y-%m-%d"
            ),
            transaction["Description"],
            *paid_by,
            *paid_for,
            transaction["Currency"],
            category,
        ]

        return row_data

    @staticmethod
    def write_to_excel(transactions, file_name):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Tricount Transactions"

        headers = [
            "Who Paid",
            "Total",
            "Currency",
            "Description",
            "When",
            "Involved",
            "File Names",
            "Attachment URLs",
            "Category",
        ]
        sheet.append(headers)

        for transaction in transactions:
            row_data = TricountHandler.prepare_transaction_data(transaction)
            sheet.append(row_data)

        workbook.save(f"{file_name}.xlsx")
        print(f"Transactions have been saved to {file_name}.xlsx.")

    @staticmethod
    def write_to_csv(transactions, file_name):
        """
        Writes transaction data to a CSV file with the given file name.

        Parameters:
        - transactions (list): A list of transaction data.
        - file_name (str): The name of the CSV file to save the data to (without the .csv extension).

        The CSV file will have the following headers:
        "Who Paid", "Total", "Currency", "Description", "When", "Involved", "File Names", "Attachment URLs", "Category"

        Each transaction will be processed by the `prepare_transaction_data` method and written to the file.
        """
        with open(f"{file_name}.csv", "w") as csvfile:
            headers = [
                "Who Paid",
                "Total",
                "Currency",
                "Description",
                "When",
                "Involved",
                "File Names",
                "Attachment URLs",
                "Category",
            ]
            transaction_writer = csv.writer(csvfile, delimiter=";")
            transaction_writer.writerow(headers)

            # Iterate through each transaction and write its data to the CSV file
            for transaction in transactions:
                row_data = TricountHandler.prepare_transaction_data(transaction)
                transaction_writer.writerow(row_data)

            print(f"Transactions have been saved to {file_name}.csv.")

    @staticmethod
    def write_to_sesterce_csv(memberships, transactions, file_name):
        """
        Writes transaction data in a specific format for Sesterce to a CSV file with the given file name.

        Parameters:
        - memberships (list): A list of memberships where each membership is a dictionary containing a "Name" key.
        - transactions (list): A list of transaction data.
        - file_name (str): The name of the CSV file to save the data to (without the .csv extension).

        The CSV file will have the following headers:
        "Date", "Title", "Paid by member" for each member, "Paid for member" for each member, "Currency", "Category"

        Each transaction will be processed by the `prepare_sesterce_transaction_data` method and written to the file.
        """
        # Sort members alphabetically based on their "Name"
        members = sorted([member["Name"] for member in memberships])

        with open(f"{file_name}.csv", "w") as csvfile:
            headers = (
                ["Date", "Title"]
                + [f"Paid by {member}" for member in members]
                + [f"Paid for {member}" for member in members]
                + ["Currency", "Category"]
            )
            transaction_writer = csv.writer(
                csvfile, delimiter=","
            )  # Sesterce expects "," delimiter
            transaction_writer.writerow(headers)

            # Iterate through each transaction and write its data to the CSV file
            for transaction in transactions:
                row_data = TricountHandler.prepare_sesterce_transaction_data(
                    transaction, members
                )
                transaction_writer.writerow(row_data)

            print(f"Transactions have been saved to {file_name}.csv.")

    @staticmethod
    def _report_image_font_monospace(size: int):
        """Prefer modern monospace fonts with Cyrillic; Cascadia / Consolas / DejaVu Sans Mono."""
        paths = []
        if os.name == "nt":
            windir = os.environ.get("WINDIR", r"C:\Windows")
            fonts_dir = os.path.join(windir, "Fonts")
            for fname in (
                "CascadiaMono.ttf",
                "CascadiaMonoPL.ttf",
                "Cascadia Code.ttf",
                "CascadiaCode.ttf",
                "consola.ttf",
                "Consola.ttf",
                "JetBrainsMono-Regular.ttf",
                "SourceCodePro-Regular.ttf",
                "FiraCode-Regular.ttf",
                "cour.ttf",
                "lucon.ttf",
            ):
                paths.append(os.path.join(fonts_dir, fname))
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                user_fonts = os.path.join(local, "Microsoft", "Windows", "Fonts")
                for fname in (
                    "CascadiaMono.ttf",
                    "CascadiaCode.ttf",
                    "JetBrainsMono-Regular.ttf",
                ):
                    paths.append(os.path.join(user_fonts, fname))
        paths.extend(
            (
                "/usr/share/fonts/truetype/jetbrains/JetBrainsMono-Regular.ttf",
                "/usr/share/fonts/truetype/adwaita-mono-fonts/AdwaitaMono-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                "/System/Library/Fonts/Supplemental/Courier New.ttf",
                "/System/Library/Fonts/Supplemental/Menlo.ttc",
                "/System/Library/Fonts/Menlo.ttc",
                "/Library/Fonts/JetBrainsMono-Regular.ttf",
            )
        )
        for path in paths:
            if not path or not os.path.isfile(path):
                continue
            try:
                if path.lower().endswith(".ttc"):
                    return ImageFont.truetype(path, size, index=0)
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _write_lines_as_image(lines, out_path, font_size=17):
        expanded = list(lines) if lines else ["(пусто)"]

        font = TricountHandler._report_image_font_monospace(font_size)
        margin = 22
        line_gap = 8
        bg = (248, 248, 248)
        fg = (24, 24, 24)
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

        def line_metrics(line: str) -> tuple[int, int]:
            left, top, right, bottom = probe.textbbox((0, 0), line, font=font)
            return right - left, bottom - top

        max_w = max(line_metrics(ln)[0] for ln in expanded)
        line_heights = [
            max(line_metrics(ln)[1], font_size) + line_gap for ln in expanded
        ]
        img_w = max(int(max_w + 2 * margin), 320)
        img_h = max(int(sum(line_heights) + 2 * margin - line_gap), 120)

        img = Image.new("RGB", (img_w, img_h), bg)
        draw = ImageDraw.Draw(img)
        y = float(margin)
        for ln, lh in zip(expanded, line_heights):
            draw.text((margin, y), ln, font=font, fill=fg)
            y += lh

        img.save(out_path, "PNG")

    @staticmethod
    def write_expenses_text_report(
        memberships,
        transactions,
        all_file_name="expenses",
        user_file_name="user",
        text_dir="expense_reports_text",
        images_dir="expense_reports_images",
    ):
        os.makedirs(text_dir, exist_ok=True)
        os.makedirs(images_dir, exist_ok=True)

        members = sorted(m["Name"] for m in memberships)
        lines = []
        member_lines = []

        def tx_date(when: str) -> str:
            return when[:10] if when else ""

        def tx_type_ru(type_transaction):
            return {
                "NORMAL": "расход",
                "BALANCE": "пер.-д",
                "INCOME": "приход",
            }.get(type_transaction, type_transaction)

        for name in members:
            lines = []
            lines.append("=" * 72)
            lines.append(f"Участник: {name}")
            lines.append("")

            lines_transfer = []
            lines_payer = []
            lines_share = []

            transfer = 0.0
            paid = 0.0
            spent = 0.0
            ccy = transactions[0]["Currency"] if transactions else ""

            sorted_tx = sorted(transactions, key=lambda t: t["When"], reverse=True)
            any_row = False
            for t in sorted_tx:
                is_transfer = t["Type"] == "BALANCE"
                is_payer = t["Who Paid"] == name
                share = float(t["Shares"].get(name, 0.0))
                has_share = share > 0
                if not is_payer and not has_share:
                    continue
                any_row = True
                ccy = t["Currency"]
                d = tx_date(t["When"])
                kind = tx_type_ru(t["Type"])
                desc = t["Description"] or "(без описания)"
                amt = t["Total"]
                if is_transfer:
                    user_me_amt = float(t["Shares"].get(name, 0.0))
                    user_other_name = ""
                    user_other_amt = 0.0
                    for share_name in t["Shares"]:
                        if share_name != name:
                            user_other_name = share_name
                            user_other_amt = float(t["Shares"].get(share_name, 0.0))
                    transfer -= user_me_amt
                    transfer += user_other_amt
                    is_income = user_me_amt > 0.0
                    lines_transfer.append(
                        f"  {(-1 if is_income else 1) * amt:9.2f} {ccy} [{d}] — {desc} — "
                        f"{'мне <-' if is_income else 'я ->'} {user_other_name}"
                    )
                if is_payer and not is_transfer:
                    paid += amt
                    lines_payer.append(f"  {amt:9.2f} {ccy} [{d} {kind}] — {desc}")
                if has_share and not is_transfer:
                    if amt < 0.0:
                        share *= -1
                    spent += share
                    payer_label = "Вас" if t["Who Paid"] == name else t["Who Paid"]
                    lines_share.append(
                        f"  {share:9.2f} {ccy} из {amt:9.2f} [{d} {kind}] от {payer_label} — {desc}"
                    )

            lines.append("Переводы:")
            lines += lines_transfer
            lines.append("")
            lines.append("Оплаты и получения:")
            lines += lines_payer
            lines.append("")
            lines.append("Участие в расходах:")
            lines += lines_share

            if not any_row:
                lines.append("  (нет операций с вашим участием)")

            bal = transfer + paid - spent
            lines.append("")
            lines.append("  Баланс:")
            lines.append(f"    переводы: {transfer:10.2f} {ccy}")
            lines.append(f"    оплаты:   {paid:10.2f} {ccy}")
            lines.append(f"    расходы:  {spent:10.2f} {ccy}")
            lines.append(f"    ИТОГО:    {bal:10.2f} {ccy}")
            lines.append("")

            member_lines.append(lines)
            txt_path = os.path.join(text_dir, f"{user_file_name} {name}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            print(f"  {name} expense report saved to {txt_path}.")
            png_path = os.path.join(images_dir, f"{user_file_name} {name}.png")
            TricountHandler._write_lines_as_image(lines, png_path)
            print(f"  {name} expense report image saved to {png_path}.")

        txt_path = os.path.join(text_dir, f"{all_file_name}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            for member_line in member_lines:
                f.write("\n".join(member_line))
        print(f"Member expense report saved to {txt_path}.")

        all_lines = []
        for member_line in member_lines:
            all_lines.extend(member_line)
        png_path = os.path.join(images_dir, f"{all_file_name}.png")
        TricountHandler._write_lines_as_image(all_lines, png_path)
        print(f"Member expense report image saved to {png_path}.")


if __name__ == "__main__":
    # example key
    tricount_key = "tISWyMCgrIMgFuxudZ"

    api = TricountAPI()
    api.authenticate()
    data = api.fetch_tricount_data(tricount_key)

    # save data to local file
    with open("response_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # load data from local file
    # with open('response_data.json', 'r', encoding="utf-8") as f:
    #    data = json.load(f)

    handler = TricountHandler()
    tricount_title = handler.get_tricount_title(data)
    memberships, transactions = handler.parse_tricount_data(data)
    # handler.write_to_csv(transactions, file_name=f"Transactions {tricount_title}")
    # handler.write_to_excel(transactions, file_name=f"Transactions {tricount_title}")
    # handler.write_to_sesterce_csv(memberships, transactions, f"Transaction {tricount_title} (Sesterce)")
    # handler.download_attachments(transactions, download_folder=f"Attachments {tricount_title}")
    handler.write_expenses_text_report(
        memberships,
        transactions,
        all_file_name=f"All expenses {tricount_title}",
        user_file_name=f"Expenses",
    )
