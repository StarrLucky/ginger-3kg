/* Сжатие снимка перед отправкой.

   Обязательно на клиенте: оригинал с 12-мегапиксельной камеры — это 3-5 МБ,
   он забивает канал Pi и раздувает счёт за токены. 1024 px по длинной стороне
   держат картинку около 1100 токенов, чего модели хватает, чтобы разглядеть
   тарелку. */

export const MAX_EDGE = 1024;
export const QUALITY = 0.7;

/** Файл из <input type="file"> -> base64 без префикса data:. */
export async function compress(file, { maxEdge = MAX_EDGE, quality = QUALITY } = {}) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
  const width = Math.round(bitmap.width * scale);
  const height = Math.round(bitmap.height * scale);

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  canvas.getContext('2d').drawImage(bitmap, 0, 0, width, height);
  // ImageBitmap держит несжатый буфер; на телефоне это десятки мегабайт.
  bitmap.close();

  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('Не удалось сжать снимок'))),
      'image/jpeg', quality);
  });

  return blobToBase64(blob);
}

export function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('Не удалось прочитать файл'));
    // result — data:image/jpeg;base64,XXXX; серверу нужна только часть после запятой
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.readAsDataURL(blob);
  });
}
