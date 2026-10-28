"use client";

import { useRef, useState } from "react";
import { FolderOpen, Layers, Plus, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
	Dialog,
	DialogBody,
	DialogContent,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { useEditor } from "@/editor/use-editor";
import {
	EXPORT_FORMAT_VALUES,
	EXPORT_QUALITY_VALUES,
	isExportFormat,
	isExportQuality,
	type ExportFormat,
	type ExportQuality,
} from "@/export";
import { DEFAULT_EXPORT_OPTIONS } from "@/export/defaults";
import { MAX_CONCURRENCY, runBatchExport, type BatchItem } from "@/batch/run";
import { findTemplateVideo, TITLE_PLACEHOLDER } from "@/batch/variant";
import { generateUUID } from "@/utils/id";
import { cn } from "@/utils/ui";

const QUALITY_LABELS: Record<ExportQuality, string> = {
	low: "Thấp",
	medium: "Vừa",
	high: "Cao",
	very_high: "Rất cao",
};

const CONCURRENCY_VALUES = Array.from(
	{ length: MAX_CONCURRENCY },
	(_, i) => i + 1,
);
const DEFAULT_CONCURRENCY = 2;

const STATUS_LABELS: Record<BatchItem["status"], string> = {
	queued: "Đang chờ",
	preparing: "Đang đọc video",
	exporting: "Đang xuất",
	done: "Xong",
	error: "Lỗi",
	cancelled: "Đã huỷ",
};

const VIDEO_PICKER_TYPES = [
	{
		description: "Video",
		accept: { "video/*": [".mp4", ".mov", ".webm", ".mkv", ".m4v"] },
	},
];

export function BatchExportButton() {
	const [isOpen, setIsOpen] = useState(false);
	const hasProject = useEditor((e) => !!e.project.getActiveOrNull());

	return (
		<>
			<Button
				variant="outline"
				size="sm"
				className="h-8 gap-1.5"
				disabled={!hasProject}
				onClick={() => setIsOpen(true)}
				title="Dùng project này làm mẫu để xuất nhiều video cùng lúc"
			>
				<Layers className="size-3.5" />
				Xuất hàng loạt
			</Button>
			{hasProject && (
				<BatchExportDialog isOpen={isOpen} onOpenChange={setIsOpen} />
			)}
		</>
	);
}

function BatchExportDialog({
	isOpen,
	onOpenChange,
}: {
	isOpen: boolean;
	onOpenChange: (open: boolean) => void;
}) {
	const editor = useEditor();
	const templateVideo = useEditor((e) =>
		findTemplateVideo({ tracks: e.scenes.getActiveScene().tracks }),
	);
	const activeProject = useEditor((e) => e.project.getActive());
	const [items, setItems] = useState<BatchItem[]>([]);
	const [directory, setDirectory] = useState<FileSystemDirectoryHandle | null>(
		null,
	);
	const [format, setFormat] = useState<ExportFormat>(
		DEFAULT_EXPORT_OPTIONS.format,
	);
	const [quality, setQuality] = useState<ExportQuality>(
		DEFAULT_EXPORT_OPTIONS.quality,
	);
	const [includeAudio, setIncludeAudio] = useState<boolean>(
		DEFAULT_EXPORT_OPTIONS.includeAudio ?? true,
	);
	const [concurrency, setConcurrency] = useState(DEFAULT_CONCURRENCY);
	const [isRunning, setIsRunning] = useState(false);
	const cancelRef = useRef(false);
	const fileInputRef = useRef<HTMLInputElement>(null);

	const addFiles = (files: File[]) => {
		const videos = files.filter(
			(file) =>
				file.type.startsWith("video/") ||
				/\.(mp4|mov|webm|mkv|m4v)$/i.test(file.name),
		);
		setItems((prev) => [
			...prev.filter((item) => item.status === "queued"),
			...videos.map((file) => ({
				id: generateUUID(),
				file,
				status: "queued" as const,
				progress: 0,
			})),
		]);
	};

	const pickVideos = async () => {
		if (!window.showOpenFilePicker) {
			fileInputRef.current?.click();
			return;
		}
		try {
			const handles = await window.showOpenFilePicker({
				multiple: true,
				types: VIDEO_PICKER_TYPES,
			});
			addFiles(await Promise.all(handles.map((handle) => handle.getFile())));
		} catch {
			// người dùng đóng hộp thoại
		}
	};

	const pickDirectory = async () => {
		if (!window.showDirectoryPicker) {
			toast.error("Trình duyệt này không hỗ trợ chọn thư mục để lưu");
			return;
		}
		try {
			setDirectory(await window.showDirectoryPicker({ mode: "readwrite" }));
		} catch {
			// người dùng đóng hộp thoại
		}
	};

	const update = ({ id, patch }: { id: string; patch: Partial<BatchItem> }) =>
		setItems((prev) =>
			prev.map((item) => (item.id === id ? { ...item, ...patch } : item)),
		);

	const start = async () => {
		if (!directory || !activeProject) return;
		const queued = items.filter((item) => item.status === "queued");
		cancelRef.current = false;
		setIsRunning(true);
		try {
			await runBatchExport({
				editor,
				items: queued,
				directory,
				options: {
					format,
					quality,
					fps: activeProject.settings.fps,
					includeAudio,
				},
				concurrency,
				onUpdate: update,
				isCancelled: () => cancelRef.current,
			});
			if (!cancelRef.current)
				toast.success(`Đã xuất xong vào thư mục “${directory.name}”`);
		} catch (error) {
			toast.error("Xuất hàng loạt thất bại", {
				description: error instanceof Error ? error.message : String(error),
			});
		} finally {
			setIsRunning(false);
		}
	};

	const queuedCount = items.filter((item) => item.status === "queued").length;

	return (
		<Dialog
			open={isOpen}
			onOpenChange={(open) => !isRunning && onOpenChange(open)}
		>
			<DialogContent className="max-w-xl">
				<DialogHeader>
					<DialogTitle>Xuất hàng loạt</DialogTitle>
				</DialogHeader>

				<DialogBody className="gap-4">
					{templateVideo ? (
						<p className="text-muted-foreground text-sm">
							Project này là <b>mẫu</b>. Video <b>{templateVideo.name}</b> trên
							track chính sẽ được thay bằng từng video bạn chọn; chữ, logo, hiệu
							ứng kéo dài tới hết video sẽ tự co giãn theo. Chữ có{" "}
							<code>{TITLE_PLACEHOLDER}</code> sẽ thành tên video.
						</p>
					) : (
						<p className="text-destructive text-sm">
							Project chưa có video nào trên track chính. Hãy thêm một video mẫu
							(video này sẽ được thay bằng từng video bạn chọn).
						</p>
					)}

					<div className="flex flex-col gap-2">
						<div className="flex items-center justify-between">
							<Label>Video cần xuất ({items.length})</Label>
							<div className="flex gap-2">
								<Button
									variant="outline"
									size="sm"
									onClick={pickVideos}
									disabled={isRunning}
								>
									<Plus className="size-3.5" /> Chọn video…
								</Button>
								{!isRunning && items.length > 0 && (
									<Button
										variant="ghost"
										size="sm"
										onClick={() => setItems([])}
									>
										Xoá hết
									</Button>
								)}
							</div>
							<input
								ref={fileInputRef}
								type="file"
								accept="video/*"
								multiple
								hidden
								onChange={(e) => {
									addFiles(Array.from(e.target.files ?? []));
									e.target.value = "";
								}}
							/>
						</div>
						<div className="flex max-h-64 flex-col gap-1 overflow-y-auto rounded-md border p-1">
							{items.length === 0 && (
								<p className="text-muted-foreground p-3 text-center text-sm">
									Chưa chọn video nào. Có thể chọn nhiều file cùng lúc (giữ
									Ctrl/Shift).
								</p>
							)}
							{items.map((item) => (
								<div
									key={item.id}
									className="flex flex-col gap-1 rounded px-2 py-1.5 hover:bg-accent/50"
								>
									<div className="flex items-center gap-2 text-sm">
										<span
											className="min-w-0 flex-1 truncate"
											title={item.file.name}
										>
											{item.file.name}
										</span>
										<span
											className={cn(
												"shrink-0 text-xs",
												item.status === "error"
													? "text-destructive"
													: "text-muted-foreground",
												item.status === "done" && "text-constructive",
											)}
										>
											{STATUS_LABELS[item.status]}
											{item.status === "exporting" &&
												` ${Math.round(item.progress * 100)}%`}
										</span>
										{item.status === "queued" && !isRunning && (
											<button
												type="button"
												className="text-muted-foreground hover:text-foreground"
												onClick={() =>
													setItems((prev) =>
														prev.filter((i) => i.id !== item.id),
													)
												}
												aria-label="Bỏ video này"
											>
												<X className="size-3.5" />
											</button>
										)}
									</div>
									{(item.status === "exporting" ||
										item.status === "preparing") && (
										<Progress value={item.progress * 100} className="h-1" />
									)}
									{item.error && (
										<p className="text-destructive text-xs">{item.error}</p>
									)}
									{item.status === "done" && item.outputName && (
										<p className="text-muted-foreground text-xs">
											→ {item.outputName}
										</p>
									)}
								</div>
							))}
						</div>
					</div>

					<div className="flex items-center gap-2">
						<Label className="shrink-0">Lưu vào</Label>
						<span className="text-muted-foreground min-w-0 flex-1 truncate text-sm">
							{directory ? directory.name : "Chưa chọn thư mục"}
						</span>
						<Button
							variant="outline"
							size="sm"
							onClick={pickDirectory}
							disabled={isRunning}
						>
							<FolderOpen className="size-3.5" /> Chọn thư mục…
						</Button>
					</div>

					<div className="flex flex-wrap gap-x-6 gap-y-3">
						<div className="flex flex-col gap-2">
							<Label>Định dạng</Label>
							<RadioGroup
								value={format}
								onValueChange={(v) => isExportFormat(v) && setFormat(v)}
								className="flex gap-3"
								disabled={isRunning}
							>
								{EXPORT_FORMAT_VALUES.map((value) => (
									<div key={value} className="flex items-center gap-1.5">
										<RadioGroupItem value={value} id={`batch-${value}`} />
										<Label htmlFor={`batch-${value}`}>
											{value.toUpperCase()}
										</Label>
									</div>
								))}
							</RadioGroup>
						</div>
						<div className="flex flex-col gap-2">
							<Label>Chất lượng</Label>
							<RadioGroup
								value={quality}
								onValueChange={(v) => isExportQuality(v) && setQuality(v)}
								className="flex gap-3"
								disabled={isRunning}
							>
								{EXPORT_QUALITY_VALUES.map((value) => (
									<div key={value} className="flex items-center gap-1.5">
										<RadioGroupItem value={value} id={`batch-q-${value}`} />
										<Label htmlFor={`batch-q-${value}`}>
											{QUALITY_LABELS[value]}
										</Label>
									</div>
								))}
							</RadioGroup>
						</div>
						<div className="flex items-center gap-2 self-end">
							<Checkbox
								id="batch-audio"
								checked={includeAudio}
								onCheckedChange={(checked) => setIncludeAudio(!!checked)}
								disabled={isRunning}
							/>
							<Label htmlFor="batch-audio">Kèm âm thanh</Label>
						</div>
						<div className="flex flex-col gap-2">
							<Label>Xuất cùng lúc</Label>
							<RadioGroup
								value={String(concurrency)}
								onValueChange={(v) => setConcurrency(Number(v))}
								className="flex gap-3"
								disabled={isRunning}
							>
								{CONCURRENCY_VALUES.map((value) => (
									<div key={value} className="flex items-center gap-1.5">
										<RadioGroupItem
											value={String(value)}
											id={`batch-c-${value}`}
										/>
										<Label htmlFor={`batch-c-${value}`}>{value} video</Label>
									</div>
								))}
							</RadioGroup>
						</div>
					</div>
				</DialogBody>

				<DialogFooter>
					{isRunning ? (
						<Button
							variant="outline"
							onClick={() => (cancelRef.current = true)}
						>
							Huỷ
						</Button>
					) : (
						<Button variant="outline" onClick={() => onOpenChange(false)}>
							Đóng
						</Button>
					)}
					<Button
						onClick={start}
						disabled={
							isRunning || !templateVideo || !directory || queuedCount === 0
						}
					>
						{isRunning ? "Đang xuất…" : `Xuất ${queuedCount} video`}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
