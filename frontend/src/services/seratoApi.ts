type UploadSeratoResponse = {
  message: string;
  size_bytes: number;
  preview: string;
  tracks: [];
};

export async function uploadSeratoFile(
  file: File
): Promise<UploadSeratoResponse> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch("http://localhost:8000/upload/serato", {
    method: "POST",
    body: formData,
  });

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || "Serato upload failed");
  }

  return data;
}
