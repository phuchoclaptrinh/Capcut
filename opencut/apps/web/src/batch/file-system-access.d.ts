// File System Access API (Chromium/WebView2) chưa có trong lib.dom của TypeScript.
// Khai báo tuỳ chọn để luôn phải kiểm tra trình duyệt có hỗ trợ trước khi gọi.

interface FilePickerAcceptType {
	description?: string;
	accept: Record<string, string[]>;
}

interface OpenFilePickerOptions {
	multiple?: boolean;
	types?: FilePickerAcceptType[];
	excludeAcceptAllOption?: boolean;
}

interface DirectoryPickerOptions {
	mode?: "read" | "readwrite";
}

interface Window {
	showOpenFilePicker?: (
		options?: OpenFilePickerOptions,
	) => Promise<FileSystemFileHandle[]>;
	showDirectoryPicker?: (
		options?: DirectoryPickerOptions,
	) => Promise<FileSystemDirectoryHandle>;
}
