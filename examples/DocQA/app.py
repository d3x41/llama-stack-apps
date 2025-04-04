# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.

import os
import subprocess
import threading
import time
import traceback
import random
from multiprocessing import freeze_support
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from llama_stack.distribution.library_client import LlamaStackAsLibraryClient
from llama_stack_client.lib.agents.agent import Agent
from llama_stack_client.lib.agents.event_logger import EventLogger
from llama_stack_client.lib.inference.utils import MessageAttachment
from llama_stack_client.types import Document

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")


def find_file_set(search_directory):
    # Common image file extensions
    file_extensions = {".txt", ".pdf", ".md", ".rst"}
    # Add uppercase versions of extensions
    file_extensions.update({ext.upper() for ext in file_extensions})

    file_path = Path(search_directory)
    return [
        str(file) for file in file_path.rglob("*.*") if file.suffix in file_extensions
    ]


class LlamaChatInterface:
    def __init__(self):
        self.docs_dir = None
        self.client = None
        self.agent = None
        self.session_id = None
        self.vector_db_id = "DocQA_Vector_DB"
        self.model_name = None

    def initialize_system(self, provider_name="ollama"):
        print(
            f"Initializing system with provider_name: {provider_name}, docs_dir: {self.docs_dir}"
        )
        self.client = LlamaStackAsLibraryClient(provider_name)
        # Remove scoring and eval providers.
        del self.client.async_client.config.providers["scoring"]
        del self.client.async_client.config.providers["eval"]
        tool_groups = []
        # only keep rag-runtime provider
        for provider in self.client.async_client.config.tool_groups:
            if provider.provider_id == "rag-runtime":
                tool_groups.append(provider)
        vector_io = []
        # only keep faiss provider
        for provider in self.client.async_client.config.providers["vector_io"]:
            if provider.provider_id == "faiss":
                vector_io.append(provider)
        assert len(vector_io) == 1
        self.client.async_client.config.tool_groups = tool_groups
        self.client.async_client.config.providers["vector_io"] = vector_io
        self.client.initialize()
        self.setup_vector_dbs()
        self.initialize_agent()

    def setup_vector_dbs(self):
        providers = self.client.providers.list()
        vector_io_provider = [
            provider for provider in providers if provider.api == "vector_io"
        ]
        provider_id = vector_io_provider[0].provider_id
        print(f"Setting up vector_dbs with provider_id: {provider_id}")
        vector_dbs = self.client.vector_dbs.list()
        if vector_dbs and any(
            bank.identifier == self.vector_db_id for bank in vector_dbs
        ):
            print(f"vector_dbs '{self.vector_db_id}' exists.")
        else:
            print(f"vector_dbs '{self.vector_db_id}' does not exist. Creating...")
            self.client.vector_dbs.register(
                vector_db_id=self.vector_db_id,
                embedding_model="all-MiniLM-L6-v2",
                embedding_dimension=384,
                provider_id=provider_id,
            )
            self.load_documents()
            print("vector_dbs registered.")

    def load_documents(self):
        documents = []
        # Load all files in the docs_dir
        print(f"Loading documents from {self.docs_dir}")
        file_set = find_file_set(self.docs_dir)
        for filename in file_set:
            if filename.endswith((".txt", ".md", ".rst")):
                file_path = os.path.join(self.docs_dir, filename)
                with open(file_path, "r", encoding="utf-8") as file:
                    content = file.read()
                document = Document(
                    document_id=filename,
                    content=content,
                    mime_type="text/plain",
                    metadata={"filename": filename},
                )
                documents.append(document)
            elif filename.endswith((".pdf")):
                file_path = os.path.join(self.docs_dir, filename)
                document = Document(
                    document_id=filename,
                    content=MessageAttachment.base64(file_path),
                    mime_type="text/plain",
                    metadata={"filename": filename},
                )
                documents.append(document)
        if documents:
            self.client.tool_runtime.rag_tool.insert(
                documents=documents,
                vector_db_id=self.vector_db_id,
                chunk_size_in_tokens=256,
            )
            print(f"Loaded {len(documents)} documents from {self.docs_dir}")

    def initialize_agent(self):
        self.agent = Agent(
            self.client,
            model=self.model_name,
            instructions="You are a helpful assistant that can answer questions based on provided documents. Return your answer short and concise, less than 50 words.",
            tools=[
                {
                    "name": "builtin::rag",
                    "args": {"vector_db_ids": [self.vector_db_id]},
                }
            ],
        )
        self.session_id = self.agent.create_session(
            "session-" + str(random.randint(0, 10000))
        )

    def chat_stream(self, message: str):
        try:
            response = self.agent.create_turn(
                messages=[{"role": "user", "content": message}],
                session_id=self.session_id,
            )
        except Exception as e:
            print(f"Error: {e}")
            yield f"Error: {e}"
            return

        current_response = ""
        for log in EventLogger().log(response):
            if hasattr(log, "content"):
                # print(f"Debug Response: {log.content}")
                if "tool_execution>" in str(log):
                    current_response += " <tool-begin> " + log.content + " <tool-end> "
                else:
                    current_response += log.content
                    yield current_response


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LlamaStack Chat")
        self.geometry("1100x800")
        self.configure(padx=20, pady=20)
        self.chat_interface = LlamaChatInterface()
        self.chat_history = []
        self.setup_completed = False
        self.is_processing = False

        # Header Frame
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(pady=(10, 20), fill="x")
        self.header_label = ctk.CTkLabel(
            self.header_frame, text="LlamaStack Chat", font=("Inter", 28, "bold")
        )
        self.header_label.pack()

        # Tabview for Setup and Chat
        self.tabview = ctk.CTkTabview(self, width=960, height=580, corner_radius=10)
        self.tabview.pack(pady=10)
        self.tabview.add("Setup")
        self.tabview.add("Chat")

        # Setup Tab
        self.setup_tab = self.tabview.tab("Setup")
        self.setup_inner_frame = ctk.CTkFrame(self.setup_tab, corner_radius=10)
        self.setup_inner_frame.pack(pady=20, padx=20, fill="both", expand=True)

        self.setup_folder_label = ctk.CTkLabel(
            self.setup_inner_frame, text="Data Folder Path:", font=("Inter", 16)
        )
        self.setup_folder_label.pack(pady=8)
        self.folder_entry = ctk.CTkEntry(
            self.setup_inner_frame, width=500, font=("Inter", 14)
        )
        self.folder_entry.pack(pady=8)
        # self.folder_entry.insert(0, DOCS_DIR)

        self.browse_button = ctk.CTkButton(
            self.setup_inner_frame,
            text="Browse",
            font=("Inter", 14),
            command=self.choose_folder,
        )
        self.browse_button.pack(pady=8)

        self.provider_label = ctk.CTkLabel(
            self.setup_inner_frame, text="Provider List:", font=("Inter", 16)
        )
        self.provider_label.pack(pady=8)
        self.provider_combobox = ctk.CTkComboBox(
            self.setup_inner_frame,
            command=self.provider_modified,
            width=400,
            font=("Inter", 14),
            values=["ollama", "together", "fireworks"],
        )
        self.provider_combobox.pack(pady=8)
        # self.provider_combobox.set("ollama")
        self.model_label = ctk.CTkLabel(
            self.setup_inner_frame, text="Llama Model Name:", font=("Inter", 16)
        )
        values = [
            "meta-llama/Llama-3.2-1B-Instruct",
            "meta-llama/Llama-3.2-3B-Instruct",
            "meta-llama/Llama-3.1-8B-Instruct",
        ]
        self.model_label.pack(pady=8)
        self.model_combobox = ctk.CTkComboBox(
            self.setup_inner_frame, width=400, font=("Inter", 14), values=values
        )
        self.model_combobox.pack(pady=8)
        self.api_label = ctk.CTkLabel(
            self.setup_inner_frame, text="API Key (if needed):", font=("Inter", 16)
        )
        self.api_label.pack(pady=8)
        self.api_entry = ctk.CTkEntry(
            self.setup_inner_frame, width=500, font=("Inter", 14), show="*"
        )
        self.api_entry.pack(pady=8)

        self.setup_button = ctk.CTkButton(
            self.setup_inner_frame,
            text="Setup Chat Interface",
            font=("Inter", 16, "bold"),
            command=self.setup_chat_interface,
            corner_radius=8,
        )
        self.setup_button.pack(pady=20)

        self.setup_status_label = ctk.CTkLabel(
            self.setup_inner_frame, text="", font=("Inter", 14)
        )
        self.setup_status_label.pack(pady=8)

        # Chat Tab
        self.chat_tab = self.tabview.tab("Chat")
        self.chat_inner_frame = ctk.CTkFrame(self.chat_tab, corner_radius=10)
        self.chat_inner_frame.pack(pady=20, padx=20, fill="both", expand=True)

        self.chat_display = ctk.CTkTextbox(
            self.chat_inner_frame,
            width=920,
            height=400,
            font=("Inter", 14),
            fg_color="white",
            text_color="black",
        )
        self.chat_display.pack(pady=10)
        self.chat_display._textbox.tag_configure(
            "user", foreground="#26d679", font=("Inter", 14, "bold")
        )
        self.chat_display._textbox.tag_configure(
            "assistant", foreground="#6c6d7b", font=("Inter", 14, "bold")
        )
        self.chat_display._textbox.tag_configure(
            "tool", foreground="#f44f4f", font=("Inter", 14, "italic")
        )
        self.chat_display.configure(state="disabled")

        self.message_entry = ctk.CTkEntry(
            self.chat_inner_frame, width=700, font=("Inter", 14)
        )
        self.message_entry.pack(pady=8)
        self.message_entry.bind("<Return>", lambda event: self.send_message())

        self.button_frame = ctk.CTkFrame(self.chat_inner_frame)
        self.button_frame.pack(pady=10)

        self.send_button = ctk.CTkButton(
            self.button_frame,
            text="Send",
            font=("Inter", 14, "bold"),
            command=self.send_message,
            corner_radius=8,
        )
        self.send_button.pack(side="left", padx=10)

        self.clear_button = ctk.CTkButton(
            self.button_frame,
            text="Clear",
            font=("Inter", 14, "bold"),
            command=self.clear_chat,
            corner_radius=8,
        )
        self.clear_button.pack(side="left", padx=10)

        self.exit_button = ctk.CTkButton(
            self.button_frame,
            text="Exit",
            font=("Inter", 14, "bold"),
            command=self.destroy,
            corner_radius=8,
        )
        self.exit_button.pack(side="left", padx=10)

    def provider_modified(self, choice):
        print("Provider modified:", self.provider_combobox.get())
        provider = self.provider_combobox.get()
        if provider == "ollama":
            values = [
                "meta-llama/Llama-3.2-1B-Instruct",
                "meta-llama/Llama-3.2-3B-Instruct",
                "meta-llama/Llama-3.1-8B-Instruct",
            ]
        else:
            values = [
                "meta-llama/Llama-3.1-8B-Instruct",
                "meta-llama/Llama-3.3-70B-Instruct",
                "meta-llama/Llama-3.1-405B-Instruct-FP8",
            ]
        self.model_combobox.set(values[0])
        self.model_combobox.configure(values=values)

    def choose_folder(self):
        folder_selected = filedialog.askdirectory(title="Select Data Folder")
        if folder_selected:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, folder_selected)

    def setup_chat_interface(self):
        docs_dir = self.folder_entry.get()
        model_name = self.model_combobox.get()
        self.chat_interface.model_name = model_name
        provider_name = self.provider_combobox.get()
        print("Start the config with", provider_name, model_name, docs_dir)
        api_key = self.api_entry.get()
        os.environ["INFERENCE_MODEL"] = (
            model_name  # Set inference model environment variable
        )

        if not os.path.exists(docs_dir):
            self.setup_status_label.configure(
                text=f"Folder {docs_dir} does not exist.", text_color="red"
            )
            return
        # setting up the chat interface
        self.chat_interface.docs_dir = docs_dir
        if provider_name == "ollama":
            ollama_name_dict = {
                "meta-llama/Llama-3.2-1B-Instruct": "llama3.2:1b-instruct-fp16",
                "meta-llama/Llama-3.2-3B-Instruct": "llama3.2:3b-instruct-fp16",
                "meta-llama/Llama-3.1-8B-Instruct": "llama3.1:8b-instruct-fp16",
            }
            if model_name not in ollama_name_dict:
                self.setup_status_label.configure(
                    text=f"Model {model_name} is not supported. Use 1B, 3B, or 8B.",
                    text_color="red",
                )
                return
            ollama_name = ollama_name_dict[model_name]
            try:
                print("Starting Ollama server...")
                subprocess.Popen(
                    f"/usr/local/bin/ollama pull all-minilm:latest".split(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.Popen(
                    f"/usr/local/bin/ollama run all-minilm:latest".split(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.Popen(
                    f"/usr/local/bin/ollama pull {ollama_name}".split(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.Popen(
                    f"/usr/local/bin/ollama run {ollama_name} --keepalive=99h".split(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(3)
            except Exception as e:
                print(e)
                print("".join(traceback.format_tb(e.__traceback__)))

                self.setup_status_label.configure(text=f"Error: {e}", text_color="red")
                return
        elif provider_name == "together":
            os.environ["TOGETHER_API_KEY"] = api_key
        elif provider_name == "fireworks":
            os.environ["FIREWORKS_API_KEY"] = api_key
        try:
            print("Initializing LlamaStack client...")
            self.chat_interface.initialize_system(provider_name)
            self.setup_status_label.configure(
                text=f"Model {model_name} started using provider {provider_name}.",
                text_color="green",
            )
            self.setup_completed = True
        except Exception as e:
            print(e)
            print("".join(traceback.format_tb(e.__traceback__)))

            self.setup_status_label.configure(
                text=f"Error during setup: {e}", text_color="red"
            )

    def send_message(self):
        if self.is_processing:
            return

        if not self.setup_completed:
            self.append_chat("System: Please complete setup first.\n")
            return

        message = self.message_entry.get().strip()
        if not message:
            return

        self.is_processing = True
        self.send_button.configure(state="disabled")
        self.message_entry.delete(0, "end")

        # Add user message to chat history and display
        self.chat_history.append({"role": "user", "content": message})
        self.update_chat_display()

        # Start processing in a separate thread
        threading.Thread(target=self.process_chat, args=(message,), daemon=True).start()

    def process_chat(self, message):
        try:
            current_response = ""
            for response in self.chat_interface.chat_stream(message):
                current_response = response
                # Update the assistant's response in chat history
                if len(self.chat_history) % 2 == 1:  # If last message was from user
                    self.chat_history.append(
                        {"role": "assistant", "content": current_response}
                    )
                else:
                    self.chat_history[-1]["content"] = current_response

                # Schedule a single update to the chat display
                self.after(100, self.update_chat_display)

        except Exception as e:
            print(e)
            print("".join(traceback.format_tb(e.__traceback__)))
            self.after(0, lambda: self.append_chat(f"\nError: {e}\n"))
        finally:
            self.after(0, self.reset_input_state)

    def reset_input_state(self):
        self.is_processing = False
        self.send_button.configure(state="normal")
        self.message_entry.focus()

    def update_chat_display(self):
        self.chat_display.configure(state="normal")
        self.chat_display._textbox.delete("1.0", "end")

        for message in self.chat_history:
            if message["role"] == "user":
                self.chat_display._textbox.insert(
                    "end", f"User: {message['content']}\n", "user"
                )
            else:
                # Check if the message contains a tool execution
                tool_execution = False
                words = message["content"].split()
                cur_message = ""
                for word in words:
                    if word.startswith("<tool-begin>"):
                        tool_execution = True
                    elif word.startswith("<tool-end>"):
                        self.chat_display._textbox.insert(
                            "end",
                            f"Tool Execution: {cur_message}\n",
                            "tool",
                        )
                        cur_message = ""
                        tool_execution = False
                    else:
                        cur_message += " " + word
                if cur_message and tool_execution:
                    self.chat_display._textbox.insert(
                        "end",
                        f"Tool Execution: {cur_message}\n",
                        "tool",
                    )
                else:
                    self.chat_display._textbox.insert(
                        "end", f"Assistant: {cur_message}\n\n", "assistant"
                    )

        self.chat_display.configure(state="disabled")
        self.chat_display._textbox.see("end")

    def clear_chat(self):
        self.chat_history = []
        self.update_chat_display()
        self.session_id = self.agent.create_session(
            "session-" + str(random.randint(0, 10000))
        )

    def append_chat(self, text):
        self.chat_display.configure(state="normal")
        self.chat_display._textbox.insert("end", text)
        self.chat_display.configure(state="disabled")
        self.chat_display._textbox.see("end")


if __name__ == "__main__":
    freeze_support()
    app = App()
    app.mainloop()
