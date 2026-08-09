using System;
using System.IO;
using System.Text;
using zkemkeeper;

class GetFacePhotoFromDevice
{
    static int Main(string[] args)
    {
        // stdout en UTF-8 : côté Python la sortie est décodée en UTF-8
        try { Console.OutputEncoding = new UTF8Encoding(false); } catch { }

        if (args.Length < 4)
        {
            Console.WriteLine("Usage: ConsoleApp1.exe <ip> <port> <pin> <outputDir> [comKey]");
            return 10;
        }

        string ip       = args[0];
        int    port     = int.Parse(args[1]);
        string pin      = args[2];
        string outputDir = args[3];
        string comKey   = args.Length >= 5 ? args[4] : "654321";

        int machineNumber = 1;
        CZKEM device = new CZKEM();

        try

        {
            // ── ComKey (communication password) ──────────────────────────────
            if (!string.IsNullOrEmpty(comKey) && comKey != "0")
            {
                device.SetCommPassword(int.Parse(comKey));
                Console.WriteLine($"🔑 ComKey appliqué : {comKey}");
            }

            // ── Connexion ─────────────────────────────────────────────────────
            Console.WriteLine($"Connexion à {ip}:{port} ...");
            if (!device.Connect_Net(ip, port))
            {
                int err = 0;
                device.GetLastError(ref err);
                Console.WriteLine($"❌ Connexion échouée. Erreur : {err}");
                return 1;
            }
            Console.WriteLine("✅ Connecté à la machine.");

            // ── Récupérer la liste des noms de photos ─────────────────────────
            string allPhotoNames = "";
            if (!device.GetUserFacePhotoNames(machineNumber, out allPhotoNames))
            {
                int err = 0;
                device.GetLastError(ref err);
                Console.WriteLine($"❌ Échec de récupération des noms de photos. Erreur : {err}");
                return 2;
            }

            // Les noms sont séparés par \t ou \n
            string[] photoNames = allPhotoNames.Split(
                new char[] { '\t', '\n' },
                StringSplitOptions.RemoveEmptyEntries
            );

            // ── Trouver la photo du PIN ───────────────────────────────────────
            string targetPhotoName = null;
            foreach (var name in photoNames)
            {
                if (name.Equals($"{pin}.jpg",     StringComparison.OrdinalIgnoreCase) ||
                    name.Equals($"{pin}_50.jpg",  StringComparison.OrdinalIgnoreCase))
                {
                    targetPhotoName = name;
                    break;
                }
            }

            if (string.IsNullOrEmpty(targetPhotoName))
            {
                Console.WriteLine($"⚠️ Aucune photo trouvée pour le PIN {pin}");
                return 3;
            }
            Console.WriteLine($"✅ Photo trouvée : {targetPhotoName}");

            // ── Télécharger les octets ────────────────────────────────────────
            byte[] photoData = new byte[1024 * 1024]; // 1 Mo
            int photoSize = 0;
            if (!device.GetUserFacePhotoByName(machineNumber, targetPhotoName, out photoData[0], out photoSize))
            {
                int err = 0;
                device.GetLastError(ref err);
                Console.WriteLine($"❌ Échec de téléchargement de la photo. Erreur : {err}");
                return 4;
            }

            // ── Sauvegarder sur le disque ─────────────────────────────────────
            string outputPath = Path.Combine(outputDir, $"verify_biophoto_9_{pin}.jpg");
            Directory.CreateDirectory(outputDir);
            using (FileStream fs = new FileStream(outputPath, FileMode.Create, FileAccess.Write))
            {
                fs.Write(photoData, 0, photoSize);
            }

            Console.WriteLine($"🖼️ Photo sauvegardée dans : {outputPath}");
            return 0;
        }
        finally
        {
            try { device.Disconnect(); } catch { }
        }
    }
}
