import type { EditorCore } from "@/core";
import type { ExportOptions, ExportResult } from "@/export";
import { getExportFileExtension } from "@/export";
import { readVideoFile } from "@/media/mediabunny";
import type { MediaAsset } from "@/media/types";
import type { TProjectSettings } from "@/project/types";
import { exportTimeline } from "@/services/renderer/timeline-export";
import { videoCache } from "@/services/video-cache/service";
import type { SceneTracks } from "@/timeline";
import { getElementFontFamilies } from "@/timeline/element-utils";
import { generateUUID } from "@/utils/id";
import {
	BATCH_CHANNEL,
	getDesktopBridge,
	toPortableAsset,
	toPortableTracks,
	type BatchMessage,
} from "./protocol";
import {
	buildBatchVariant,
	findTemplateVideo,
	titleFromFileName,
} from "./variant";

export type BatchItemStatus =
	| "queued"
	| "preparing"
	| "exporting"
	| "done"
	| "error"
	| "cancelled";

export interface BatchItem {
	id: string;
	file: File;
	status: BatchItemStatus;
	progress: number;
	outputName?: string;
	error?: string;
}

export const MAX_CONCURRENCY = 4;

/** Chờ cửa sổ xuất khởi động (nạp trang + WebGL) tối đa chừng này. */
const WORKER_STARTUP_TIMEOUT_MS = 30_000;

type ExportSettings = Pick<
	TProjectSettings,
	"fps" | "canvasSize" | "background"
>;

interface PreparedItem {
	asset: MediaAsset;
	tracks: SceneTracks;
	outputName: string;
	handle: FileSystemFileHandle;
}

/** Xuất một video đã chuẩn bị, trả về kết quả xuất. */
type Exporter = ({
	prepared,
	onProgress,
}: {
	prepared: PreparedItem;
	onProgress: ({ progress }: { progress: number }) => void;
}) => Promise<ExportResult>;

async function exists({
	directory,
	name,
}: {
	directory: FileSystemDirectoryHandle;
	name: string;
}): Promise<boolean> {
	try {
		await directory.getFileHandle(name);
		return true;
	} catch {
		return false;
	}
}

/**
 * "Tên.mp4" -> "Tên (2).mp4"... để không bao giờ ghi đè file có sẵn (kể cả video gốc).
 * `reserved` giữ chỗ các tên đang xuất dở để hai video chạy song song không trùng tên.
 */
async function uniqueName({
	directory,
	base,
	extension,
	reserved,
}: {
	directory: FileSystemDirectoryHandle;
	base: string;
	extension: string;
	reserved: Set<string>;
}): Promise<string> {
	for (let i = 1; ; i++) {
		const name = i === 1 ? `${base}${extension}` : `${base} (${i})${extension}`;
		if (reserved.has(name) || (await exists({ directory, name }))) continue;
		// Kiểm tra lại sau await: video khác có thể vừa lấy tên này
		if (reserved.has(name)) continue;
		reserved.add(name);
		return name;
	}
}

/**
 * Đọc video thành media tạm thời. Không dùng processMediaAssets vì hàm đó đòi đủ chỗ
 * lưu file trong trình duyệt, trong khi xuất hàng loạt không lưu video vào project.
 */
async function readAsset({ file }: { file: File }): Promise<MediaAsset> {
	const data = await readVideoFile({ file });
	if (!data.canDecode) {
		throw new Error(
			`Trình duyệt không giải mã được codec ${data.codec ?? "này"}. Hãy dùng video H.264 MP4.`,
		);
	}
	return {
		id: generateUUID(),
		name: file.name,
		type: "video",
		file,
		url: URL.createObjectURL(file),
		thumbnailUrl: data.thumbnailUrl ?? undefined,
		duration: data.duration,
		width: data.width,
		height: data.height,
		fps: Number.isFinite(data.fps) ? Math.round(data.fps) : undefined,
		hasAudio: data.hasAudio,
		ephemeral: true,
	};
}

/** Xuất ngay trong cửa sổ này (các luồng chỉ thay phiên nhau trên một luồng chính). */
function localExporter({
	baseAssets,
	settings,
	options,
	isCancelled,
}: {
	baseAssets: MediaAsset[];
	settings: ExportSettings;
	options: ExportOptions;
	isCancelled: () => boolean;
}): Exporter {
	return async ({ prepared, onProgress }) =>
		exportTimeline({
			tracks: prepared.tracks,
			mediaAssets: [...baseAssets, prepared.asset],
			settings,
			options,
			stream: await prepared.handle.createWritable(),
			onProgress,
			onCancel: isCancelled,
		});
}

/**
 * Nhờ app desktop mở các cửa sổ xuất riêng. Trả về null nếu không chạy trong app
 * hoặc không cửa sổ nào khởi động được (khi đó xuất ngay trong cửa sổ này).
 */
async function startRemoteWorkers({ count }: { count: number }): Promise<{
	channel: BroadcastChannel;
	workerIds: string[];
	close: () => Promise<void>;
} | null> {
	const bridge = getDesktopBridge();
	if (!bridge || count < 2) return null;

	const channel = new BroadcastChannel(BATCH_CHANNEL);
	const ready = new Set<string>();
	const onMessage = (event: MessageEvent<BatchMessage>) => {
		if (event.data.type === "worker-ready") ready.add(event.data.workerId);
	};
	channel.addEventListener("message", onMessage);
	const close = async () => {
		channel.close();
		await bridge.close_export_workers();
	};

	try {
		const opened = await bridge.open_export_workers(count);
		const deadline = Date.now() + WORKER_STARTUP_TIMEOUT_MS;
		while (ready.size < opened && Date.now() < deadline) {
			channel.postMessage({ type: "hello" } satisfies BatchMessage);
			await new Promise((resolve) => setTimeout(resolve, 300));
		}
	} finally {
		channel.removeEventListener("message", onMessage);
	}

	if (ready.size === 0) {
		await close();
		return null;
	}
	return { channel, workerIds: [...ready], close };
}

/** Gửi một video sang cửa sổ xuất `workerId` và chờ kết quả. */
function remoteExporter({
	channel,
	workerId,
	baseAssets,
	settings,
	options,
	isCancelled,
}: {
	channel: BroadcastChannel;
	workerId: string;
	baseAssets: MediaAsset[];
	settings: ExportSettings;
	options: ExportOptions;
	isCancelled: () => boolean;
}): Exporter {
	return ({ prepared, onProgress }) =>
		new Promise<ExportResult>((resolve) => {
			const jobId = generateUUID();
			const onMessage = (event: MessageEvent<BatchMessage>) => {
				const message = event.data;
				if (!("jobId" in message) || message.jobId !== jobId) return;
				if (message.type === "progress")
					onProgress({ progress: message.progress });
				if (message.type === "result") {
					channel.removeEventListener("message", onMessage);
					clearInterval(cancelTimer);
					resolve({
						success: message.success,
						cancelled: message.cancelled,
						error: message.error,
					});
				}
			};
			const cancelTimer = setInterval(() => {
				if (isCancelled()) {
					channel.postMessage({ type: "cancel", jobId } satisfies BatchMessage);
				}
			}, 200);
			channel.addEventListener("message", onMessage);
			channel.postMessage({
				type: "job",
				workerId,
				job: {
					jobId,
					tracks: toPortableTracks({ tracks: prepared.tracks }),
					mediaAssets: [...baseAssets, prepared.asset].map((asset) =>
						toPortableAsset({ asset }),
					),
					fontFamilies: getElementFontFamilies({ tracks: prepared.tracks }),
					settings,
					options,
					output: prepared.handle,
				},
			} satisfies BatchMessage);
		});
}

/**
 * Xuất các video theo mẫu là project đang mở, `concurrency` video cùng lúc.
 * Project đang mở không bị thay đổi: mỗi video được dựng thành một timeline riêng trong bộ nhớ.
 */
export async function runBatchExport({
	editor,
	items,
	directory,
	options,
	concurrency,
	onUpdate,
	isCancelled,
}: {
	editor: EditorCore;
	items: BatchItem[];
	directory: FileSystemDirectoryHandle;
	options: ExportOptions;
	concurrency: number;
	onUpdate: ({ id, patch }: { id: string; patch: Partial<BatchItem> }) => void;
	isCancelled: () => boolean;
}): Promise<void> {
	const tracks = editor.scenes.getActiveScene().tracks;
	const templateVideo = findTemplateVideo({ tracks });
	if (!templateVideo) {
		throw new Error(
			"Project mẫu cần có ít nhất một video trên track chính (video này sẽ được thay bằng từng video mới).",
		);
	}
	const activeProject = editor.project.getActive();
	const settings: ExportSettings = activeProject.settings;
	const baseAssets = editor.media.getAssets();
	const extension = getExportFileExtension({ format: options.format });
	const reserved = new Set<string>();
	const lanes = Math.max(
		1,
		Math.min(concurrency, MAX_CONCURRENCY, items.length),
	);

	const exportOne = async ({
		item,
		exporter,
	}: {
		item: BatchItem;
		exporter: Exporter;
	}) => {
		if (isCancelled()) {
			onUpdate({ id: item.id, patch: { status: "cancelled" } });
			return;
		}

		let asset: MediaAsset | null = null;
		let outputName: string | null = null;
		try {
			onUpdate({ id: item.id, patch: { status: "preparing", progress: 0 } });
			asset = await readAsset({ file: item.file });

			const title = titleFromFileName({ name: item.file.name });
			const variant = buildBatchVariant({
				tracks,
				templateVideo,
				asset,
				title,
			});
			outputName = await uniqueName({
				directory,
				base: title,
				extension,
				reserved,
			});
			const handle = await directory.getFileHandle(outputName, {
				create: true,
			});
			onUpdate({ id: item.id, patch: { status: "exporting", outputName } });

			const result = await exporter({
				prepared: { asset, tracks: variant, outputName, handle },
				onProgress: ({ progress }) =>
					onUpdate({ id: item.id, patch: { progress } }),
			});

			if (result.cancelled) throw new DOMException("cancelled", "AbortError");
			if (!result.success) throw new Error(result.error ?? "Xuất thất bại");
			onUpdate({ id: item.id, patch: { status: "done", progress: 1 } });
		} catch (error) {
			const cancelled =
				error instanceof DOMException && error.name === "AbortError";
			onUpdate({
				id: item.id,
				patch: {
					status: cancelled ? "cancelled" : "error",
					error: cancelled
						? undefined
						: error instanceof Error
							? error.message
							: String(error),
				},
			});
			// Xoá file dở dang
			if (outputName) await directory.removeEntry(outputName).catch(() => {});
		} finally {
			if (asset) {
				videoCache.clearVideo({ mediaId: asset.id });
				if (asset.url) URL.revokeObjectURL(asset.url);
			}
		}
	};

	// Mỗi luồng lấy video tiếp theo trong hàng đợi cho tới khi hết
	const queue = [...items];
	const runLane = async ({ exporter }: { exporter: Exporter }) => {
		for (let item = queue.shift(); item; item = queue.shift()) {
			await exportOne({ item, exporter });
		}
	};

	const remote = await startRemoteWorkers({ count: lanes });
	try {
		const exporters = remote
			? remote.workerIds.map((workerId) =>
					remoteExporter({
						channel: remote.channel,
						workerId,
						baseAssets,
						settings,
						options,
						isCancelled,
					}),
				)
			: Array.from({ length: lanes }, () =>
					localExporter({ baseAssets, settings, options, isCancelled }),
				);
		await Promise.all(exporters.map((exporter) => runLane({ exporter })));
	} finally {
		await remote?.close();
	}
}
