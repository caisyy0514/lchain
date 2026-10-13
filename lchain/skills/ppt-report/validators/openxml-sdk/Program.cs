using System.IO.Compression;
using System.Text;
using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using DocumentFormat.OpenXml.Validation;

namespace Medex.OpenXmlSdkValidator;

internal static class Program
{
    private const string SupportedTarget = "Office2016";
    private const int MaxReportedErrors = 100;
    private const int MaxZipEntries = 20_000;
    private const long MaxPackageBytes = 384L * 1024 * 1024;
    private const long MaxEntryBytes = 256L * 1024 * 1024;
    private const long MaxExpandedBytes = 1024L * 1024 * 1024;

    private const int ExitSuccess = 0;
    private const int ExitUsage = 2;
    private const int ExitInput = 3;
    private const int ExitInvalidPackage = 4;
    private const int ExitInternal = 5;

    public static int Main(string[] args)
    {
        if (!TryParseArguments(args, out var target, out var filePath, out var argumentError))
        {
            Console.Error.WriteLine($"ARGUMENT_ERROR: {argumentError}");
            WriteUsage();
            return ExitUsage;
        }

        if (!string.Equals(target, SupportedTarget, StringComparison.Ordinal))
        {
            Console.Error.WriteLine(
                $"ARGUMENT_ERROR: unsupported target '{Sanitize(target)}'; expected {SupportedTarget}");
            return ExitUsage;
        }

        try
        {
            var input = ValidateInput(filePath);
            using var stream = new FileStream(
                input.FullName,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read,
                bufferSize: 64 * 1024,
                options: FileOptions.SequentialScan);

            ValidateZipEnvelope(stream);
            stream.Position = 0;

            var settings = new OpenSettings { AutoSave = false };
            using var document = PresentationDocument.Open(stream, false, settings);
            var validator = new OpenXmlValidator(FileFormatVersions.Office2016)
            {
                MaxNumberOfErrors = MaxReportedErrors + 1,
            };

            var errorCount = 0;
            foreach (var error in validator.Validate(document))
            {
                errorCount++;
                if (errorCount <= MaxReportedErrors)
                {
                    WriteValidationError(errorCount, error);
                }
            }

            if (errorCount != 0)
            {
                if (errorCount > MaxReportedErrors)
                {
                    Console.Error.WriteLine(
                        $"ERRORS_TRUNCATED: reported={MaxReportedErrors} total-at-least={errorCount}");
                }

                Console.Error.WriteLine(
                    errorCount > MaxReportedErrors
                        ? $"INVALID target={SupportedTarget} errors=>={errorCount}"
                        : $"INVALID target={SupportedTarget} errors={errorCount}");
                return ExitInvalidPackage;
            }

            Console.Out.WriteLine($"VALID target={SupportedTarget} errors=0");
            return ExitSuccess;
        }
        catch (InputRejectedException exception)
        {
            Console.Error.WriteLine($"INPUT_ERROR: {Sanitize(exception.Message)}");
            return ExitInput;
        }
        catch (UnauthorizedAccessException exception)
        {
            Console.Error.WriteLine($"INPUT_ERROR: {Sanitize(exception.Message)}");
            return ExitInput;
        }
        catch (IOException exception)
        {
            Console.Error.WriteLine($"INPUT_ERROR: {Sanitize(exception.Message)}");
            return ExitInput;
        }
        catch (InvalidDataException exception)
        {
            Console.Error.WriteLine($"INVALID_PACKAGE: {Sanitize(exception.Message)}");
            return ExitInvalidPackage;
        }
        catch (OpenXmlPackageException exception)
        {
            Console.Error.WriteLine($"INVALID_PACKAGE: {Sanitize(exception.Message)}");
            return ExitInvalidPackage;
        }
        catch (System.Xml.XmlException exception)
        {
            Console.Error.WriteLine($"INVALID_PACKAGE: {Sanitize(exception.Message)}");
            return ExitInvalidPackage;
        }
        catch (FormatException exception)
        {
            Console.Error.WriteLine($"INVALID_PACKAGE: {Sanitize(exception.Message)}");
            return ExitInvalidPackage;
        }
        catch (NotSupportedException exception)
        {
            Console.Error.WriteLine($"INVALID_PACKAGE: {Sanitize(exception.Message)}");
            return ExitInvalidPackage;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(
                $"INTERNAL_ERROR: {Sanitize(exception.GetType().Name)}: {Sanitize(exception.Message)}");
            return ExitInternal;
        }
    }

    private static bool TryParseArguments(
        string[] args,
        out string target,
        out string filePath,
        out string error)
    {
        target = string.Empty;
        filePath = string.Empty;
        error = string.Empty;

        if (args.Length != 4)
        {
            error = "exactly --target <value> and --file <path> are required";
            return false;
        }

        for (var index = 0; index < args.Length; index += 2)
        {
            var option = args[index];
            var value = args[index + 1];
            if (string.IsNullOrWhiteSpace(value))
            {
                error = $"{option} requires a non-empty value";
                return false;
            }

            switch (option)
            {
                case "--target" when target.Length == 0:
                    target = value;
                    break;
                case "--file" when filePath.Length == 0:
                    filePath = value;
                    break;
                case "--target" or "--file":
                    error = $"duplicate option {option}";
                    return false;
                default:
                    error = $"unknown option {Sanitize(option)}";
                    return false;
            }
        }

        if (target.Length == 0 || filePath.Length == 0)
        {
            error = "both --target and --file are required";
            return false;
        }

        return true;
    }

    private static FileInfo ValidateInput(string filePath)
    {
        if (!Path.IsPathFullyQualified(filePath))
        {
            throw new InputRejectedException("--file must be an absolute path");
        }

        var fullPath = Path.GetFullPath(filePath);
        if (!string.Equals(Path.GetExtension(fullPath), ".pptx", StringComparison.OrdinalIgnoreCase))
        {
            throw new InputRejectedException("--file must have a .pptx extension");
        }

        var input = new FileInfo(fullPath);
        if (!input.Exists)
        {
            throw new InputRejectedException("input file does not exist");
        }

        if ((input.Attributes & FileAttributes.Directory) != 0)
        {
            throw new InputRejectedException("input path is not a regular file");
        }

        if ((input.Attributes & FileAttributes.ReparsePoint) != 0 || input.LinkTarget is not null)
        {
            throw new InputRejectedException("symbolic links are not accepted");
        }

        if (input.Length <= 0 || input.Length > MaxPackageBytes)
        {
            throw new InputRejectedException(
                $"input size must be between 1 and {MaxPackageBytes} bytes");
        }

        return input;
    }

    private static void ValidateZipEnvelope(Stream stream)
    {
        using var archive = new ZipArchive(stream, ZipArchiveMode.Read, leaveOpen: true);
        if (archive.Entries.Count == 0 || archive.Entries.Count > MaxZipEntries)
        {
            throw new InvalidDataException(
                $"ZIP entry count must be between 1 and {MaxZipEntries}");
        }

        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var expandedBytes = 0L;
        foreach (var entry in archive.Entries)
        {
            ValidateEntryName(entry.FullName);
            if (!names.Add(entry.FullName))
            {
                throw new InvalidDataException($"duplicate ZIP entry: {Sanitize(entry.FullName)}");
            }

            if (entry.Length < 0 || entry.Length > MaxEntryBytes)
            {
                throw new InvalidDataException(
                    $"ZIP entry exceeds the {MaxEntryBytes}-byte limit: {Sanitize(entry.FullName)}");
            }

            if (expandedBytes > MaxExpandedBytes - entry.Length)
            {
                throw new InvalidDataException(
                    $"expanded ZIP size exceeds the {MaxExpandedBytes}-byte limit");
            }

            expandedBytes += entry.Length;
        }

        if (!names.Contains("[Content_Types].xml") || !names.Contains("_rels/.rels"))
        {
            throw new InvalidDataException("required Open Packaging Convention parts are missing");
        }
    }

    private static void ValidateEntryName(string entryName)
    {
        if (string.IsNullOrEmpty(entryName) || entryName.Length > 1024)
        {
            throw new InvalidDataException("ZIP entry name is empty or too long");
        }

        if (entryName.StartsWith("/", StringComparison.Ordinal)
            || entryName.Contains('\\')
            || entryName.Split('/').Any(segment => segment == ".."))
        {
            throw new InvalidDataException($"unsafe ZIP entry name: {Sanitize(entryName)}");
        }
    }

    private static void WriteValidationError(int index, ValidationErrorInfo error)
    {
        var part = error.Part?.Uri?.OriginalString ?? "(package)";
        var path = error.Path?.XPath ?? "(unknown)";
        var id = error.Id ?? "(unknown)";
        var description = error.Description ?? "(no description)";
        Console.Error.WriteLine(
            $"ERROR {index}: id={Sanitize(id)} part={Sanitize(part)} "
            + $"path={Sanitize(path)} description={Sanitize(description)}");
    }

    private static string Sanitize(string? value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return "(empty)";
        }

        const int maxLength = 2_000;
        var length = Math.Min(value.Length, maxLength);
        var singleLine = new StringBuilder(length + 3);
        for (var index = 0; index < length; index++)
        {
            var character = value[index];
            singleLine.Append(char.IsControl(character) ? ' ' : character);
        }

        if (value.Length > maxLength)
        {
            singleLine.Append("...");
        }

        return singleLine.ToString();
    }

    private static void WriteUsage()
    {
        Console.Error.WriteLine(
            "Usage: medex-openxml-sdk-validator --target Office2016 --file /absolute/path/file.pptx");
    }

    private sealed class InputRejectedException : Exception
    {
        public InputRejectedException(string message)
            : base(message)
        {
        }
    }
}
