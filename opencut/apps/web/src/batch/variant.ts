import type { MediaAsset } from "@/media/types";
import type {
	SceneTracks,
	TimelineElement,
	TimelineTrack,
	VideoElement,
} from "@/timeline";
import {
	addMediaTime,
	mediaTimeFromSeconds,
	subMediaTime,
	ZERO_MEDIA_TIME,
	type MediaTime,
} from "@/wasm";

export const TITLE_PLACEHOLDER = "{title}";

/** Lớp kết thúc cách cuối video mẫu trong khoảng này được coi là "kéo dài tới hết video". */
const END_TOLERANCE_SECONDS = 0.25;

/** Video chính của mẫu: clip video dài nhất trên track chính. */
export function findTemplateVideo({
	tracks,
}: {
	tracks: SceneTracks;
}): VideoElement | null {
	let best: VideoElement | null = null;
	for (const element of tracks.main.elements) {
		if (element.type !== "video") continue;
		if (!best || element.duration > best.duration) best = element;
	}
	return best;
}

/** Tên file -> tiêu đề: bỏ đuôi và mã ngôn ngữ kiểu " [vi]" do YT Downloader thêm vào. */
export function titleFromFileName({ name }: { name: string }): string {
	return name.replace(/\.[^.]+$/, "").replace(/\s*\[[A-Za-z0-9-]+\]$/, "");
}

function endOf(element: TimelineElement): MediaTime {
	return addMediaTime({ a: element.startTime, b: element.duration });
}

function withTitle<T extends TimelineElement>({
	element,
	title,
}: {
	element: T;
	title: string;
}): T {
	if (element.type !== "text") return element;
	const content = element.params.content;
	if (typeof content !== "string" || !content.includes(TITLE_PLACEHOLDER)) {
		return element;
	}
	return {
		...element,
		params: {
			...element.params,
			content: content.replaceAll(TITLE_PLACEHOLDER, title),
		},
	};
}

/**
 * Tạo timeline cho một video mới từ timeline mẫu.
 *
 * - Video chính được thay bằng `asset` (phát trọn video mới).
 * - Lớp kéo dài tới hết video mẫu (chữ, logo...) được kéo dài/rút ngắn theo video mới.
 * - Lớp nằm sau video mẫu (outro) được dời theo.
 * - Lớp nằm giữa video giữ nguyên, bị cắt nếu video mới ngắn hơn.
 * - `{title}` trong chữ được thay bằng `title`.
 */
export function buildBatchVariant({
	tracks,
	templateVideo,
	asset,
	title,
}: {
	tracks: SceneTracks;
	templateVideo: VideoElement;
	asset: MediaAsset;
	title: string;
}): SceneTracks {
	if (!asset.duration) {
		throw new Error(`Không đọc được thời lượng của ${asset.name}`);
	}

	const sourceDuration = mediaTimeFromSeconds({ seconds: asset.duration });
	const rate = templateVideo.retime?.rate ?? 1;
	const newDuration = mediaTimeFromSeconds({ seconds: asset.duration / rate });
	const oldEnd = endOf(templateVideo);
	const newEnd = addMediaTime({ a: templateVideo.startTime, b: newDuration });
	const delta = subMediaTime({ a: newEnd, b: oldEnd });
	const tolerance = mediaTimeFromSeconds({ seconds: END_TOLERANCE_SECONDS });

	const adjust = <T extends TimelineElement>({
		element,
	}: {
		element: T;
	}): T | null => {
		if (element.id === templateVideo.id) {
			return {
				...element,
				mediaId: asset.id,
				name: asset.name,
				trimStart: ZERO_MEDIA_TIME,
				trimEnd: ZERO_MEDIA_TIME,
				sourceDuration,
				duration: newDuration,
			};
		}

		const start = element.startTime;
		const end = endOf(element);

		// Nằm sau video mẫu (outro): dời theo
		if (start >= oldEnd - tolerance && start > templateVideo.startTime) {
			return withTitle({
				element: {
					...element,
					startTime: addMediaTime({ a: start, b: delta }),
				},
				title,
			});
		}

		// Kéo dài tới hết video mẫu: kéo dài/rút ngắn theo video mới.
		// Video/âm thanh khác không kéo dài quá nguồn của nó.
		const reachesEnd = end >= oldEnd - tolerance;
		const hasSource = element.type === "video" || element.type === "audio";
		let targetEnd: MediaTime = reachesEnd && !hasSource ? newEnd : end;
		if (targetEnd > newEnd) targetEnd = newEnd;
		if (targetEnd <= start) return null;

		return withTitle({
			element: {
				...element,
				duration: subMediaTime({ a: targetEnd, b: start }),
			},
			title,
		});
	};

	const adjustTrack = <T extends TimelineTrack>({
		track,
	}: {
		track: T;
	}): T => ({
		...track,
		elements: track.elements
			.map((element: TimelineElement) => adjust({ element }))
			.filter((element) => element !== null),
	});

	return {
		main: adjustTrack({ track: tracks.main }),
		overlay: tracks.overlay.map((track) => adjustTrack({ track })),
		audio: tracks.audio.map((track) => adjustTrack({ track })),
	};
}
