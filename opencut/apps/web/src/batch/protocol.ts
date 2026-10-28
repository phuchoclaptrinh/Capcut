import type { ExportOptions } from "@/export";
import type { MediaAsset } from "@/media/types";
import type { TProjectSettings } from "@/project/types";
import type { SceneTracks } from "@/timeline";

/**
 * Xuất song song thật: mỗi video được xuất trong một cửa sổ riêng (tiến trình trình duyệt riêng)
 * mở trang /batch-worker. Cửa sổ trình chỉnh sửa điều phối qua BroadcastChannel.
 * Các cửa sổ xuất do app desktop (YT Downloader) mở qua window.pywebview.api.
 */
export const BATCH_CHANNEL = "opencut-batch-export";

/** Media gửi sang cửa sổ khác: object URL không dùng được ở trang khác nên bỏ đi. */
export type PortableMediaAsset = Omit<MediaAsset, "url" | "thumbnailUrl">;

export interface BatchJob {
	jobId: string;
	tracks: SceneTracks;
	mediaAssets: PortableMediaAsset[];
	fontFamilies: string[];
	settings: Pick<TProjectSettings, "fps" | "canvasSize" | "background">;
	options: ExportOptions;
	output: FileSystemFileHandle;
}

export type BatchMessage =
	| { type: "hello" }
	| { type: "worker-ready"; workerId: string }
	| { type: "job"; workerId: string; job: BatchJob }
	| { type: "cancel"; jobId: string }
	| { type: "progress"; jobId: string; progress: number }
	| {
			type: "result";
			jobId: string;
			success: boolean;
			cancelled?: boolean;
			error?: string;
	  };

export interface DesktopBridge {
	open_export_workers: (count: number) => Promise<number>;
	close_export_workers: () => Promise<void>;
}

export function getDesktopBridge(): DesktopBridge | null {
	const api = (
		window as Window & { pywebview?: { api?: Partial<DesktopBridge> } }
	).pywebview?.api;
	const open = api?.open_export_workers;
	const close = api?.close_export_workers;
	if (!api || !open || !close) return null;
	return {
		open_export_workers: (count) => open.call(api, count),
		close_export_workers: () => close.call(api),
	};
}

export function toPortableAsset({
	asset,
}: {
	asset: MediaAsset;
}): PortableMediaAsset {
	const { url: _url, thumbnailUrl: _thumbnailUrl, ...rest } = asset;
	return rest;
}

/** AudioBuffer không gửi qua BroadcastChannel được; trang xuất sẽ tự giải mã lại từ file. */
export function toPortableTracks({
	tracks,
}: {
	tracks: SceneTracks;
}): SceneTracks {
	return {
		...tracks,
		audio: tracks.audio.map((track) => ({
			...track,
			elements: track.elements.map(
				({ buffer: _buffer, ...element }) => element,
			),
		})),
	};
}
